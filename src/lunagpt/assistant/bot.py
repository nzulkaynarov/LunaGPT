"""Telegram message handling for Luna AI Assistant."""

import asyncio
import html
import logging
import re
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    Document,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    PhotoSize,
    ReactionTypeCustomEmoji,
    ReactionTypeEmoji,
    ReactionTypePaid,
    ReplyKeyboardMarkup,
    VideoNote,
    Voice,
)
from aiogram.utils.chat_action import ChatActionSender
from aiohttp import ClientError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lunagpt.assistant import access, updates
from lunagpt.assistant.about import about_luna, is_about_question, owner_name
from lunagpt.assistant.calendar_desk import CALENDAR_PREFIXES, CalendarDesk, is_calendar_request
from lunagpt.assistant.gcal import GoogleCalendar
from lunagpt.assistant.list_desk import LIST_PREFIXES, ListDesk
from lunagpt.assistant.lists import is_list_request, quoted
from lunagpt.assistant.llm import (
    LIST_TOOL,
    LISTS_BLOCK,
    LISTS_HINT,
    REMINDER_HINT,
    REMINDER_TOOLS,
    REMINDERS_BLOCK,
    SCHEDULED_TASK,
    Action,
    QuizQuestion,
    ReplyOptions,
    transcribe_voice,
)
from lunagpt.assistant.memories import FAMILY_DUPLICATE, FAMILY_FAILED, FAMILY_FULL, FAMILY_SAVED, Memory
from lunagpt.assistant.people import PEOPLE_PREFIXES, PeopleDesk
from lunagpt.assistant.phrases import PHRASE_PREFIXES, PhraseDesk
from lunagpt.assistant.pi_client import PiClient, PiConflictError, PiNotFoundError, PiUnavailableError
from lunagpt.assistant.reminder_desk import REMINDER_PREFIXES, ReminderDesk
from lunagpt.assistant.reminders import is_reminder_request
from lunagpt.assistant.school_desk import SCHOOL_PREFIXES, SchoolDesk, is_school_text, photo_kind
from lunagpt.assistant.speech import (
    SpokenReply,
    asks_for_voice,
    is_bare_voice_request,
    speak_reply,
    without_voice_refusals,
    without_voice_words,
)
from lunagpt.assistant.stories import ADULT_STORY_PREFIXES, PHRASE_ASK, STORY_PREFIXES, StoryDesk
from lunagpt.assistant.telegram import is_reply_to_prompt, remove_menu, send_long, send_model_reply
from lunagpt.assistant.waiting import Waiting, cat_is_thinking
from lunagpt.config import settings
from lunagpt.models import AssistantMember

logger = logging.getLogger("lunagpt.assistant.bot")

AnswerFn = Callable[[str, int, list[dict[str, str]], str, list[str], ReplyOptions], Awaitable[str]]
QuizFn = Callable[[int, int | None], Awaitable[QuizQuestion | None]]
TranscribeFn = Callable[[str, int, bytes, Callable[[float], None] | None, bool], Awaitable[str | None]]
SpeakFn = Callable[[str], Awaitable[SpokenReply | None]]

# The menu: one button per part of life; what is inside opens as inline buttons.
BTN_DAY = "🗓 Мой день"
BTN_LISTS = "🛒 Списки"
BTN_MEMORY = "🧠 Память"
BTN_INTERNET = "⏳ Интернет"
BTN_FAMILY = "🏠 Семья"
BTN_HELP = "❓ Помощь"
MENU_BUTTONS = {BTN_DAY, BTN_LISTS, BTN_MEMORY, BTN_INTERNET, BTN_FAMILY, BTN_HELP}
# Inside those menus. The texts are also accepted as typed buttons, since older menus carried them.
BTN_ASK_TIME = "🙏 Попросить время"
BTN_QUIZ = "🎯 Викторина +15 мин"
BTN_PEOPLE = "👥 Люди"
BTN_INVITE = "➕ Пригласить"
BTN_STORY = "🐈 Истории про Луну"
BTN_PHRASE = "😺 Фраза для Луны"
# Menus stay on a phone until a message brings a new one, so the old buttons keep working until then.
OLD_BUTTONS = {
    "🔔 Напоминания": BTN_DAY,
    "🧠 Что ты помнишь": BTN_MEMORY,
    "⏳ Моё время": BTN_INTERNET,
    "🧠 Викторина +15 мин": BTN_QUIZ,
    "🐈 Про Луну": BTN_STORY,
}
OLD_SHARED = "📺 Общие устройства"
SHARED_MOVED = (
    "📺 Общие устройства теперь в боте HomeSec, кнопка «📺 Общие устройства»: там те же действия. "
    "Я занимаюсь личными делами, а домашней сетью управляет HomeSec."
)
MEMORY_PAGE_SIZE = 8
TIME_POLL_SECONDS = 30
TIME_REQUEST_TTL = timedelta(hours=24)
FAMILY_MENU = "🏠 Семья: люди и заявки, приглашения. Телевизор и другие общие устройства — в боте HomeSec."
PI_OFFLINE = "Сейчас домашняя сеть не отвечает, попробуй чуть позже."
NO_DEVICE = "Я не нашла твоё устройство в домашней сети. Попроси родителей привязать его."


def _clock(value: str | None) -> str:
    return datetime.fromisoformat(value).strftime("%H:%M") if value else ""


def describe_restriction(restriction: dict[str, Any] | None) -> str:
    """One honest line about why the home internet is off for this person now."""
    if not restriction:
        return ""
    if restriction["kind"] == "bedtime":
        text = f"🌙 Сейчас отбой, интернет дома включится в {_clock(restriction['until'])}."
        if restriction.get("pause_until"):
            text += f" Потом ещё пауза до {_clock(restriction['pause_until'])}."
        return text
    if restriction.get("open_until"):
        return (
            f"🔓 Родители открыли интернет до {_clock(restriction['open_until'])}, "
            f"потом пауза до {_clock(restriction['until'])}."
        )
    return f"⏸ Интернет дома на паузе до {_clock(restriction['until'])}."


BEDTIME_GOODNIGHT = " Утром всё заработает само.\n\n😴 Луна уже спит клубочком, и тебе пора. Спокойной ночи!"
# First, and asking for the first person: placed after the facts, the local model told 2 of 8 stories as
# Luna's own; placed before them, 7 of 8.
STORY_MEMORIES = (
    "Мои воспоминания. Их рассказала семья про настоящую домашнюю кошку Луну, это и есть я. "
    "Когда просят историю обо мне или спрашивают, помню ли я что-то, расскажи подходящую историю отсюда "
    "от первого лица («я залезла…»), а не со стороны, и можешь сказать, кто напомнил:\n{stories}\n\n"
)
HISTORY_MESSAGES = 8
RATE_LIMIT = 10
UPDATE_CHECK_SECONDS = 600
RESTARTED_TEXT = updates.RESTARTED
RATE_WINDOW_SECONDS = 60
ACCESS_ENDED = "Срок доступа закончился. Попроси взрослого из семьи его продлить."
TOO_FAST = "🐾 Помедленнее, я не успеваю за мышкой! Подожди минутку."
TEXT_ONLY = "Пока я понимаю текст, голосовые, кружки и фото."
PHOTO_DEFAULT = "Что на фото? Если там задание, помоги с ним."
PHOTO_FAILED = "Не получилось открыть фото. Пришли его ещё раз."
PHOTO_TOO_BIG = "Слишком большой файл. Пришли фото обычным способом, не файлом."
MAX_PHOTO_BYTES = 10 * 1024 * 1024
PHOTO_TYPES = ("image/jpeg", "image/png", "image/webp")
MATH_TEXT = re.compile(
    r"\d\s*[-+*/×÷:=]\s*\d|\b(?:задач\w*|уравнени\w*|пример\w*|реши\w*|посчитай|сколько будет)\b", re.I
)
THINKING_REACTION = "👀"
DONE_REACTION = "🤗"  # Telegram allows only its own reaction set, which has no cat
# Telegram sends "what cat is this?" and the photo as two messages, in either order: a question a minute
# before a captionless photo becomes its caption, and the photo stays attached to follow-up questions.
QUESTION_BEFORE_PHOTO_SECONDS = 60.0
PHOTO_KEPT_SECONDS = 600.0
NO_VIDEO = "Видео я не смотрю. Пришли голосовое или кружок, или напиши текстом."
MAX_VOICE_SECONDS = 120
VOICE_TOO_LONG = "Слишком длинное голосовое. Запиши покороче, до двух минут."
VOICE_FAILED = "Не получилось разобрать голосовое. Попробуй ещё раз или напиши текстом."
NOTHING_TO_SAY = "Спроси что-нибудь, и я отвечу голосом."
CANNOT_SAY = "Этот ответ вслух не прочитать: он длинный или со ссылками. Спроси покороче."
VOICE_NOT_HEARD = "Не расслышала слов в этом голосовом. Попробуй ещё раз."
MEMORY_NOTICE = " Запоминаю важное о тебе, посмотреть и удалить можно в «🧠 Память». Другие люди этого не видят."
FAMILY_NOTICE = " Чтобы запомнить для всех в семье, напиши: «запомни для семьи, что…»."
WHOSE = "👤 — видишь только ты, 🏠 — видит вся семья."
ASK_ANYTHING = "Просто напиши или скажи голосом, что нужно: вопрос, идея, перевод, свежие новости."
PHOTO_HINT = "📸 Пришли фото задачи, чека или текста, и я разберу.\n\n"
SCHOOL_HINT = "📚 Фото расписания с подписью «расписание» или «задали …» запомню и напомню вовремя.\n\n"
DAY_LINE = f"{BTN_DAY} — напоминания, поручения и утренняя сводка: «напомни завтра в 9…», «присылай каждый день…»\n"
LISTS_LINE = f"{BTN_LISTS} — 🏠 семейные и 👤 личные: «добавь молоко в покупки», «добавь в мой список…»\n"
MEMORY_LINE = f"{BTN_MEMORY} — 👤 что я помню о тебе, 🏠 общая память семьи и 🐈 истории про Луну\n"
ADULT_HELP = (
    f"{ASK_ANYTHING}\n\n{PHOTO_HINT}"
    + DAY_LINE
    + LISTS_LINE
    + MEMORY_LINE
    + f"{BTN_FAMILY} — люди и приглашения\n\n"
    + WHOSE
    + "\n\n"
    + MEMORY_NOTICE.strip()
    + FAMILY_NOTICE
)
HELP_TEXT = {
    "owner": ADULT_HELP,
    "adult": ADULT_HELP,
    "kid": (
        "Спрашивай что угодно: уроки, игры, код, идеи, свежие новости.\n\n"
        + PHOTO_HINT
        + SCHOOL_HINT
        + f"{BTN_INTERNET} — сколько осталось, попросить минут у родителей и 🎯 викторина\n"
        + f"{BTN_DAY} — напоминания, расписание уроков и домашка\n"
        + LISTS_LINE
        + MEMORY_LINE.rstrip("\n")
        + ", там же 😺 фраза для Луны\n\n"
        + WHOSE
        + "\n\n"
        + MEMORY_NOTICE.strip()
        + FAMILY_NOTICE
    ),
    "guest": (
        f"{ASK_ANYTHING}\n\n{PHOTO_HINT}{BTN_DAY} — напоминания и утренняя сводка\n"
        f"{BTN_MEMORY} — что я о тебе помню\n\n" + MEMORY_NOTICE.strip()
    ),
}
WELCOME = "<b>Добро пожаловать, {name}!</b> 🌙\n\n{help}"
BACK_AGAIN = "Привет, {name}! Я на связи. Напиши, что нужно, или выбери кнопку внизу."
FAMILY_REQUEST = re.compile(
    r"^\s*(?:запомни|запиши|сохрани)\s+(?:для\s+(?:всей\s+)?(?:семьи|всех)|в\s+(?:семейн\w+|общ\w+)\s+памят\w+)"
    r"[\s:,—-]*(?:что\s+)?(?P<fact>\S.*)$",
    re.IGNORECASE | re.DOTALL,
)
FAMILY_PROMPT = (
    "Напиши ответом на это сообщение, что запомнить для всей семьи. "
    "Это увидят все в семье, включая детей, поэтому не пиши пароли и номера карт."
)
FAMILY_REPLIES = {
    FAMILY_SAVED: "Запомнила для всей семьи 🏠",
    FAMILY_DUPLICATE: "Это уже есть в общей памяти.",
    FAMILY_FULL: "Общая память заполнена. Удали что-нибудь лишнее и попробуй снова.",
    FAMILY_FAILED: "Не получилось сохранить, попробуй чуть позже.",
}
SAFETY_PATTERN = re.compile(
    r"суицид|покончить с собой|убить себя|не хочу жить|порезать себя|"
    r"меня бьют|меня бьёт|меня бьет|меня трогает|домогает|меня травят|буллят|шантажиру",
    re.IGNORECASE,
)
SAFETY_REPLY = (
    "Спасибо, что написал мне об этом. Это важно, и ты не один 💛\n"
    "Пожалуйста, расскажи прямо сейчас родителям или другому взрослому, которому доверяешь."
)


def family_request(text: str) -> str | None:
    """The note someone explicitly asked to keep for the family, if this message is such a request."""
    match = FAMILY_REQUEST.match(text)
    return match.group("fact").strip() if match else None


def google_calendar() -> GoogleCalendar | None:
    """The calendar client when the owner has added the service account key; a broken key keeps it off."""
    if not settings.luna_google_service_account:
        return None
    try:
        return GoogleCalendar.from_json(settings.luna_google_service_account, settings.luna_utc_offset_hours)
    except (ValueError, KeyError):
        logger.warning("LUNA_GOOGLE_SERVICE_ACCOUNT is not a valid service account key")
        return None


def photo_of(message: Message) -> PhotoSize | Document | None:
    """The picture in a message: the largest size of a photo, or an image sent as a file."""
    if message.photo:
        return message.photo[-1]
    document = message.document
    if document is not None and document.mime_type in PHOTO_TYPES:
        return document
    return None


def needs_adult_attention(text: str) -> bool:
    return bool(SAFETY_PATTERN.search(text))


def number_of(args: dict[str, Any]) -> int | None:
    value = args.get("number")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def menu_layout(role: str) -> list[list[str]]:
    adults = [[BTN_DAY, BTN_LISTS], [BTN_MEMORY, BTN_FAMILY], [BTN_HELP]]
    layouts = {
        "owner": adults,
        "adult": adults,
        "kid": [[BTN_INTERNET, BTN_DAY], [BTN_LISTS, BTN_MEMORY], [BTN_HELP]],
        "guest": [[BTN_DAY, BTN_MEMORY], [BTN_HELP]],
    }
    return layouts[role]


def _inline(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def family_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_inline(BTN_PEOPLE, "fhp"), _inline(BTN_INVITE, "fhi")],
        ]
    )


def internet_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_inline(BTN_ASK_TIME, "nta")], [_inline(BTN_QUIZ, "ntq")]])


def main_keyboard(role: str) -> ReplyKeyboardMarkup:
    """The persistent menu; server-side checks still decide what each role may do."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=text) for text in row] for row in menu_layout(role)],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Напиши Luna…",
    )


def quiz_keyboard(options: list[str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=o[:60], callback_data=f"quiz:{i}")] for i, o in enumerate(options)]
    )


class LunaBot:
    """Routes each private message by the sender's membership and role."""

    def __init__(
        self,
        bot: Bot,
        session_factory: async_sessionmaker[AsyncSession],
        answer: AnswerFn,
        make_quiz: QuizFn,
        pi: PiClient,
        bot_username: str,
        memory: Memory | None = None,
        transcribe: TranscribeFn = transcribe_voice,
        speak: SpeakFn = speak_reply,
    ) -> None:
        self._bot = bot
        self._transcribe = transcribe
        self._speak = speak
        self._sessions = session_factory
        self._answer = answer
        self._make_quiz = make_quiz
        self._pi = pi
        self._username = bot_username
        self._memory = memory
        self._people = PeopleDesk(bot, session_factory, pi, bot_username, self._welcome)
        calendar = google_calendar()
        self.calendar = CalendarDesk(bot, session_factory, calendar)
        self.school = SchoolDesk(bot, session_factory, mirror=self.calendar)
        self.reminders = ReminderDesk(
            bot, session_factory, extra_brief=self._brief_extra, run_task=self._run_task, calendar=calendar is not None
        )
        self.phrases = PhraseDesk(bot, session_factory)
        self.stories = StoryDesk(bot, session_factory)
        self.lists = ListDesk(bot, session_factory)
        self._history_loaded: set[int] = set()
        self._background: set[asyncio.Task[None]] = set()
        self._history: dict[int, deque[dict[str, str]]] = defaultdict(lambda: deque(maxlen=HISTORY_MESSAGES))
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._request_times: dict[int, deque[float]] = defaultdict(lambda: deque(maxlen=RATE_LIMIT))
        self._quizzes: dict[int, QuizQuestion] = {}
        self._last_question: dict[int, tuple[str, float]] = {}
        self._last_photo: dict[int, tuple[bytes, str, float]] = {}  # in memory only, never stored

    async def _send(
        self,
        chat_id: int,
        text: str,
        reply_markup: ReplyKeyboardMarkup | InlineKeyboardMarkup | None = None,
    ) -> None:
        await send_long(self._bot, chat_id, text, reply_markup)

    async def announce_update(self) -> int:
        """Tell everyone whose menu or news changed, in the daytime; returns how many were told."""
        if not updates.is_daytime():
            return 0
        async with self._sessions() as db:
            people = list(await db.scalars(select(AssistantMember)))
        told = 0
        for member in people:
            current = updates.version(member.role, menu_layout(member.role))
            if member.seen_version == current or access.access_ended(member):
                continue
            restart = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🔄 Перезапустить Луну", callback_data=updates.UPDATE_PREFIX)]
                ]
            )
            try:
                await self._bot.send_message(member.telegram_id, updates.notice(member.role), reply_markup=restart)
                told += 1
            except TelegramAPIError as exc:
                logger.warning("could not announce an update: %s", type(exc).__name__)
            await self._mark_seen(member.telegram_id, current)  # one notice per update, even if this one failed
        return told

    async def _mark_seen(self, telegram_id: int, version: str) -> None:
        async with self._sessions() as db:
            await db.execute(
                update(AssistantMember).where(AssistantMember.telegram_id == telegram_id).values(seen_version=version)
            )
            await db.commit()

    async def watch_updates(self) -> None:
        while True:
            try:
                await self.announce_update()
            except Exception:
                logger.exception("could not announce the update")
            await asyncio.sleep(UPDATE_CHECK_SECONDS)

    async def _restart(self, callback: CallbackQuery, member: AssistantMember) -> None:
        await remove_menu(callback)
        await self._send(member.telegram_id, RESTARTED_TEXT, main_keyboard(member.role))

    async def _welcome(self, telegram_id: int, name: str, role: str) -> None:
        try:
            await self._bot.send_message(
                telegram_id,
                WELCOME.format(name=html.escape(name), help=html.escape(HELP_TEXT[role], quote=False)),
                parse_mode="HTML",
                reply_markup=main_keyboard(role),
            )
        except TelegramAPIError as exc:
            logger.warning("could not welcome a new member: %s", type(exc).__name__)
        try:  # the welcome carries today's menu, so no update notice should follow it
            await self._mark_seen(telegram_id, updates.version(role, menu_layout(role)))
        except Exception:
            logger.warning("could not mark the welcome as seen", exc_info=True)

    async def on_message(self, message: Message) -> None:
        if message.chat.type != "private" or message.from_user is None:
            return
        user_id = message.from_user.id
        text = (message.text or "").strip()

        async with self._sessions() as db:
            member = await access.get_member(db, user_id)
        if member is None:
            await self._people.on_stranger(message)
            return
        if member.role in access.ADULT_ROLES and self._people.is_edit_reply(message):
            await self._people.apply_edit(member, message)
            await self._drop_prompt(message)
            return
        if member.role in access.ADULT_ROLES and self._people.is_search_reply(message):
            await self._people.search(user_id, message)
            await self._drop_prompt(message)
            return
        picture = photo_of(message)
        if picture is not None and member.role in access.FAMILY_ROLES and self.stories.is_reply(message):
            await self.stories.tell(member, (message.caption or "").strip(), picture.file_id)
            await self._drop_prompt(message)
            return
        if picture is not None:
            await self._photo(member, (message.caption or "").strip(), picture, message.message_id)
            return
        spoken_note = message.voice or message.video_note
        if not text and spoken_note is not None:
            text = await self._voice_text(member, spoken_note) or ""
            if not text:
                return
        if not text:
            await self._send(user_id, NO_VIDEO if message.video else TEXT_ONLY)
            return
        if member.role in access.FAMILY_ROLES and is_reply_to_prompt(message, FAMILY_PROMPT):
            await self._chat(member, text, family_note=text)
            await self._drop_prompt(message)
            return
        if member.role in access.FAMILY_ROLES and self.phrases.is_reply(message):
            await self.phrases.suggest(member, text)
            await self._drop_prompt(message)
            return
        if member.role in access.FAMILY_ROLES and self.stories.is_reply(message):
            await self.stories.tell(member, text)
            await self._drop_prompt(message)
            return
        if member.role in access.FAMILY_ROLES and self.lists.is_add_reply(message):
            await self.lists.add_from_reply(member, message, text)
            await self._drop_prompt(message)
            return
        if self.reminders.is_city_reply(message):
            await self.reminders.set_city(member, text)
            await self._drop_prompt(message)
            return
        if self.calendar.is_link_reply(message):
            await self.calendar.link(member, text)
            await self._drop_prompt(message)
            return
        if await self._menu_action(member, text):
            return
        if text.startswith("/start"):
            await self._send(user_id, BACK_AGAIN.format(name=member.name), main_keyboard(member.role))
            return
        if text.startswith("/"):
            await self._send(user_id, HELP_TEXT[member.role], main_keyboard(member.role))
            return
        if is_reminder_request(text):
            await self._new_reminder(member, text)
            return
        if is_calendar_request(text):
            if await self._may_ask(member):
                async with ChatActionSender.typing(chat_id=user_id, bot=self._bot):
                    await self.calendar.request_event(member, text)
            return
        if member.role == "kid" and is_school_text(text):
            if await self._may_ask(member):
                async with ChatActionSender.typing(chat_id=user_id, bot=self._bot):
                    await self.school.handle_text(member, text)
            return
        if member.role in access.FAMILY_ROLES and is_list_request(text) and not self._over_rate_limit(user_id):
            async with ChatActionSender.typing(chat_id=user_id, bot=self._bot):
                said = await self.lists.request(member, text)
            if said is not None:
                self._allow_request(user_id)  # counts like a chat turn; anything else goes on to the chat
                await self._keep_turn(member, text, said)
                return
        if spoken_note is None and is_bare_voice_request(text):
            await self._say_last_reply(member)
            return
        if asks_for_voice(text):
            await self._chat(member, without_voice_words(text), speak=True)
            return
        await self._chat(member, text, speak=spoken_note is not None, message_id=message.message_id)

    async def _drop_prompt(self, message: Message) -> None:
        """A "reply to this message" prompt has done its job once answered, so it leaves the chat."""
        prompt = message.reply_to_message
        if prompt is None or prompt.from_user is None or not prompt.from_user.is_bot:
            return
        try:
            await self._bot.delete_message(message.chat.id, prompt.message_id)
        except TelegramAPIError as exc:
            logger.info("could not remove an answered prompt: %s", type(exc).__name__)

    async def _menu_action(self, member: AssistantMember, text: str) -> bool:
        """Run the pressed menu button if this role is allowed to use it."""
        text = OLD_BUTTONS.get(text, text)
        adult = member.role in access.ADULT_ROLES
        family = member.role in access.FAMILY_ROLES
        kid = member.role == "kid"
        if text == BTN_HELP:
            await self._send(member.telegram_id, HELP_TEXT[member.role], main_keyboard(member.role))
        elif text == BTN_DAY:
            await self._send(member.telegram_id, *await self.reminders.view(member))
        elif text == BTN_MEMORY:
            await self._send(member.telegram_id, *await self._memory_view(member, 0))
        elif text == BTN_LISTS and family:
            await self.lists.view(member)
        elif text == BTN_FAMILY and adult:
            await self._send(member.telegram_id, FAMILY_MENU, family_keyboard())
        elif text == BTN_PEOPLE and adult:
            await self._people.send_summary(member.telegram_id)
        elif text == BTN_INVITE and adult:
            await self._people.send_invite(member.telegram_id)
        elif text == OLD_SHARED and adult:
            await self._send(member.telegram_id, SHARED_MOVED)
        elif text == BTN_INTERNET and kid:
            await self._my_time(member)
        elif text == BTN_ASK_TIME and kid:
            await self._ask_time(member, "")
        elif text == BTN_QUIZ and kid:
            await self._start_quiz(member)
        elif text == BTN_STORY and family:
            await self.stories.view(member)
        elif text == BTN_PHRASE and kid:
            await self.phrases.ask(member)
        else:
            return False
        return True

    async def on_callback(self, callback: CallbackQuery) -> None:
        user_id = callback.from_user.id
        async with self._sessions() as db:
            member = await access.get_member(db, user_id)
        data = callback.data or ""
        prefix = data.split(":", 1)[0]
        if member is None or not self._callback_allowed(member.role, prefix):
            await callback.answer("Нет доступа", show_alert=True)
            return
        await callback.answer()
        args = data.split(":")[1:]
        if prefix == updates.UPDATE_PREFIX:
            await self._restart(callback, member)
        elif prefix == "fhp":
            await self._edit(callback, *await self._people.summary())
        elif prefix == "fhi":
            await remove_menu(callback)
            await self._people.send_invite(user_id)
        elif prefix == "nta":
            await self._ask_time(member, "")
        elif prefix == "ntq":
            await self._start_quiz(member)
        elif prefix == PHRASE_ASK:
            await self.phrases.ask(member)
            await remove_menu(callback)
        elif prefix == "quiz":
            await self._answer_quiz(member, int(args[0]))
        elif prefix in ("sh", "shs", "shl"):  # buttons of a device list sent before the move to HomeSec
            await self._edit(callback, SHARED_MOVED, None)
        elif prefix == "mem":
            await self._edit(callback, *await self._memory_view(member, int(args[0])))
        elif prefix == "memd":
            await self._forget_fact(callback, member, args[0], int(args[1]))
        elif prefix == "memc":
            await self._edit(callback, *self._confirm_forget_all())
        elif prefix == "memy":
            await self._forget_everything(callback, user_id)
        elif prefix == "fam":
            await self._edit(callback, *await self._family_view(member, int(args[0])))
        elif prefix == "fama":
            await self._bot.send_message(user_id, FAMILY_PROMPT, reply_markup=ForceReply())
            await remove_menu(callback)
        elif prefix == "famd":
            await self._edit(callback, *await self._confirm_family_delete(member, args[0]))
        elif prefix == "famy":
            await self._forget_family_fact(callback, member, args[0])
        elif prefix in PEOPLE_PREFIXES:
            await self._people.handle(callback, member, prefix, args)
        elif prefix in REMINDER_PREFIXES:
            await self.reminders.handle(callback, member, prefix, args)
        elif prefix in SCHOOL_PREFIXES:
            await self.school.handle(callback, member, prefix, args)
        elif prefix in CALENDAR_PREFIXES:
            await self.calendar.handle(callback, member, prefix, args)
        elif prefix in PHRASE_PREFIXES:
            await self.phrases.handle(callback, member, prefix, args)
        elif prefix in STORY_PREFIXES:
            await self.stories.handle(callback, member, prefix, args)
        elif prefix in LIST_PREFIXES:
            await self.lists.handle(callback, member, prefix, args)

    @staticmethod
    def _callback_allowed(role: str, prefix: str) -> bool:
        if prefix == updates.UPDATE_PREFIX:
            return role in access.ROLES
        if prefix in ("quiz", "nta", "ntq", PHRASE_ASK):
            return role == "kid"
        if prefix in ("fhp", "fhi"):
            return role in access.ADULT_ROLES
        if (
            prefix in ("mem", "memd", "memc", "memy")
            or prefix in REMINDER_PREFIXES | SCHOOL_PREFIXES | CALENDAR_PREFIXES
        ):
            return role in access.ROLES
        if prefix in ("fam", "fama", "famd", "famy"):
            return role in access.FAMILY_ROLES
        if prefix in ADULT_STORY_PREFIXES:
            return role in access.ADULT_ROLES
        if prefix in STORY_PREFIXES or prefix in LIST_PREFIXES:
            return role in access.FAMILY_ROLES
        if prefix in PEOPLE_PREFIXES or prefix in PHRASE_PREFIXES or prefix in ("sh", "shs", "shl"):
            return role in access.ADULT_ROLES
        return False

    @staticmethod
    async def _edit(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None) -> None:
        if isinstance(callback.message, Message):
            await callback.message.edit_text(text, reply_markup=markup)

    async def _answer_quiz(self, member: AssistantMember, choice: int) -> None:
        quiz = self._quizzes.pop(member.telegram_id, None)
        if quiz is None:
            await self._send(member.telegram_id, "Этот вопрос уже закрыт.")
            return
        await self._send(member.telegram_id, await self._quiz_outcome(member, quiz, choice == quiz.correct))

    async def _quiz_outcome(self, member: AssistantMember, quiz: QuizQuestion, correct: bool) -> str:
        try:
            minutes = await self._pi.quiz_result(member.name, correct, quiz.topic)
        except PiConflictError:
            return "Сегодня викторина уже пройдена. Приходи завтра!"
        except (PiNotFoundError, PiUnavailableError):
            return PI_OFFLINE
        if correct:
            return f"🎉 Верно! Тебе начислено +{minutes} минут."
        return f"Не совсем. Правильный ответ: {quiz.options[quiz.correct]}. Завтра будет новый шанс!"

    async def _my_time(self, member: AssistantMember) -> None:
        try:
            restriction = await self._pi.restriction(member.name)
            rows = await self._pi.quota(member.name)
        except PiNotFoundError:
            await self._send(member.telegram_id, NO_DEVICE)
            return
        except PiUnavailableError:
            await self._send(member.telegram_id, PI_OFFLINE)
            return
        lines = [describe_restriction(restriction)] if restriction else []
        lines += [
            f"{r['device']}, {r['category_label']}: {r['used_minutes']} из {r['limit_minutes']} мин" for r in rows
        ]
        if not rows:
            lines.append("Лимитов на сегодня нет.")
        await self._send(member.telegram_id, "⏳ Интернет\n\n" + "\n".join(lines), internet_keyboard())

    async def _ask_time(self, member: AssistantMember, reason: str) -> None:
        user_id = member.telegram_id
        try:
            restriction = await self._pi.restriction(member.name)
        except PiUnavailableError:
            await self._send(user_id, PI_OFFLINE)
            return
        if restriction and restriction["kind"] == "bedtime":
            # Parents do not open bedtime at night, so nobody is woken up for it.
            await self._send(user_id, describe_restriction(restriction) + BEDTIME_GOODNIGHT)
            return
        try:
            request_id = await self._pi.request_time(member.name, reason)
        except PiConflictError:
            await self._send(user_id, "Запрос уже отправлен, дождись ответа родителей.")
            return
        except PiNotFoundError:
            await self._send(user_id, NO_DEVICE)
            return
        except PiUnavailableError:
            await self._send(user_id, PI_OFFLINE)
            return
        async with self._sessions() as db:
            await access.track_time_request(db, request_id, user_id)
        if restriction:
            text = "✅ Попросила родителей открыть интернет. Сообщу, когда они ответят."
            await self._send(user_id, f"{describe_restriction(restriction)}\n\n{text}")
            return  # quiz minutes would not lift a pause
        text = "✅ Запрос отправлен родителям. Сообщу, когда они ответят."
        quiz = None
        try:
            if await self._pi.quiz_available(member.name):
                text += f"\n\nПока ждёшь, можно заработать +15 минут: нажми «{BTN_QUIZ}»."
                quiz = InlineKeyboardMarkup(inline_keyboard=[[_inline(BTN_QUIZ, "ntq")]])
        except (PiNotFoundError, PiUnavailableError):
            pass
        await self._send(user_id, text, quiz)

    async def _start_quiz(self, member: AssistantMember) -> None:
        user_id = member.telegram_id
        try:
            available = await self._pi.quiz_available(member.name)
        except PiNotFoundError:
            await self._send(user_id, NO_DEVICE)
            return
        except PiUnavailableError:
            await self._send(user_id, PI_OFFLINE)
            return
        if not available:
            await self._send(user_id, "Сегодня викторина уже пройдена. Приходи завтра!")
            return
        quiz = self._quizzes.get(user_id) or await self._make_quiz(user_id, access.age_of(member, self._today()))
        if quiz is None:
            await self._send(user_id, "Не получилось придумать вопрос, попробуй чуть позже.")
            return
        self._quizzes[user_id] = quiz
        await self._send(user_id, f"🎯 {quiz.topic}\n\n{quiz.q}", quiz_keyboard(quiz.options))

    def _over_rate_limit(self, user_id: int) -> bool:
        times = self._request_times[user_id]
        return len(times) == RATE_LIMIT and time.monotonic() - times[0] < RATE_WINDOW_SECONDS

    def _allow_request(self, user_id: int) -> bool:
        if self._over_rate_limit(user_id):
            return False
        self._request_times[user_id].append(time.monotonic())
        return True

    async def _photo(
        self, member: AssistantMember, caption: str, picture: PhotoSize | Document, message_id: int | None = None
    ) -> None:
        user_id = member.telegram_id
        if (picture.file_size or 0) > MAX_PHOTO_BYTES:
            await self._send(user_id, PHOTO_TOO_BIG)
            return
        try:
            data = await self._bot.download(picture)
        except (TelegramAPIError, ClientError, TimeoutError) as exc:
            logger.warning("could not download a photo: %s", type(exc).__name__)
            data = None
        if data is None:
            await self._send(user_id, PHOTO_FAILED)
            return
        image_type = picture.mime_type if isinstance(picture, Document) and picture.mime_type else "image/jpeg"
        kind = photo_kind(caption) if member.role == "kid" else None  # timetables and homework are for children
        if kind is None:
            question = caption or self._recent_question(user_id) or PHOTO_DEFAULT
            await self._chat(member, question, image=data.read(), image_type=image_type, message_id=message_id)
            return
        if not await self._may_ask(member):
            return
        async with ChatActionSender.typing(chat_id=user_id, bot=self._bot):
            read = self.school.read_schedule if kind == "schedule" else self.school.read_tasks
            await read(member, caption, data.read(), image_type)

    async def _voice_text(self, member: AssistantMember, voice: Voice | VideoNote) -> str | None:
        """Turn a voice note or the sound of a round video into text and show it, or explain why not."""
        user_id = member.telegram_id
        today = self._today()
        refusal = None
        if self._over_rate_limit(user_id):
            refusal = TOO_FAST
        elif access.access_ended(member):
            refusal = ACCESS_ENDED
        elif voice.duration > MAX_VOICE_SECONDS:
            refusal = VOICE_TOO_LONG
        if refusal is not None:
            await self._send(user_id, refusal)
            return None
        costs: list[float] = []
        try:
            async with ChatActionSender.typing(chat_id=user_id, bot=self._bot):
                audio = await self._bot.download(voice)
                words = (
                    await self._transcribe(
                        member.role,
                        user_id,
                        audio.read(),
                        costs.append if member.role == "guest" else None,
                        access.money_limit_reached(member, today),
                    )
                    if audio is not None
                    else None
                )
        except (TelegramAPIError, ClientError, TimeoutError) as exc:
            logger.warning("could not download a voice note: %s", type(exc).__name__)
            words = None
        if any(costs):
            async with self._sessions() as db:
                await access.record_spend(db, user_id, sum(costs), today)
        if not words:
            await self._send(user_id, VOICE_FAILED if words is None else VOICE_NOT_HEARD)
            return None
        await self._send(user_id, f"🗣 «{words}»")  # a misheard word is visible before the answer
        return words

    def _recent_question(self, user_id: int) -> str | None:
        """The text sent just before a captionless photo, which is usually a question about it."""
        said, at = self._last_question.pop(user_id, ("", 0.0))
        return said if said and time.monotonic() - at < QUESTION_BEFORE_PHOTO_SECONDS else None

    def _recent_photo(self, user_id: int) -> tuple[bytes, str] | None:
        kept = self._last_photo.get(user_id)
        if kept is None:
            return None
        if time.monotonic() - kept[2] >= PHOTO_KEPT_SECONDS:
            del self._last_photo[user_id]
            return None
        return kept[0], kept[1]

    @staticmethod
    def _today() -> date:
        return (datetime.now(UTC) + timedelta(hours=settings.luna_utc_offset_hours)).date()

    async def _chat(
        self,
        member: AssistantMember,
        text: str,
        family_note: str | None = None,
        speak: bool = False,
        image: bytes | None = None,
        image_type: str = "image/jpeg",
        message_id: int | None = None,
        scheduled: bool = False,
    ) -> None:
        user_id = member.telegram_id
        if not scheduled and not self._allow_request(user_id):
            await self._send(user_id, TOO_FAST)
            return
        if not scheduled and member.role == "kid" and needs_adult_attention(text):
            await self._alert_parents(member, text)
            await self._send(user_id, SAFETY_REPLY)
            return
        if member.role in access.FAMILY_ROLES and image is None and not scheduled:
            family_note = family_note or family_request(text)
        if family_note is not None:
            await self._save_family_note(member, family_note)
            return
        async with self._sessions() as db:
            current = await access.get_member(db, user_id)
        if current is None:
            return
        if access.access_ended(current):
            await self._send(user_id, ACCESS_ENDED)
            return
        today = self._today()
        costs: list[float] = []
        earlier_photo = False
        if image is None:
            self._last_question[user_id] = (text, time.monotonic())
            recent = self._recent_photo(user_id)
            if recent is not None:
                image, image_type = recent
                earlier_photo = True
        else:
            self._last_question.pop(user_id, None)
            self._last_photo[user_id] = (image, image_type, time.monotonic())
        waiting = await self._waiting(member, text, speak, image is not None)
        await self._react(user_id, message_id, THINKING_REACTION)
        async with self._locks[user_id]:
            async with (
                ChatActionSender.typing(chat_id=user_id, bot=self._bot),  # repeats while the model works
                cat_is_thinking(self._bot, user_id, waiting),
            ):
                history = without_voice_refusals(await self._conversation(member))
                facts = await self._memory.recall(user_id, member.role, text) if self._memory else []
                options = ReplyOptions(
                    age=access.age_of(current, today),
                    local_only=access.money_limit_reached(current, today),
                    on_cost=costs.append if current.role == "guest" else None,
                    family_facts=await self._memory.family.recall(current.role, text) if self._memory else (),
                    spoken=speak,
                    image=image,
                    image_type=image_type,
                    earlier_photo=earlier_photo,
                    planner=await self._planner(current),
                    on_search=waiting.searching,
                    about=await self._about(current, text),
                    actions=await self._actions(current),
                )
                reply = await self._answer(member.role, user_id, history, text, facts, options)
            # Later turns keep only a mark of the photo: its bytes are never stored or sent again.
            said = f"[фото] {text}" if image is not None and not earlier_photo else text
            self._history[user_id].append({"role": "user", "content": said})
            self._history[user_id].append({"role": "assistant", "content": reply})
            if self._memory:
                await self._memory.save_exchange(user_id, said, reply, HISTORY_MESSAGES)
        if any(costs):
            async with self._sessions() as db:
                await access.record_spend(db, user_id, sum(costs), today)
        await send_model_reply(self._bot, user_id, reply)
        await self._react(user_id, message_id, DONE_REACTION if member.role == "kid" else None)
        if speak:
            await self._send_voice_reply(user_id, reply)
        if self._memory and text != PHOTO_DEFAULT and not scheduled:
            task = asyncio.create_task(self._memory.remember(user_id, member.role, text))
            self._background.add(task)  # the event loop keeps only weak references to tasks
            task.add_done_callback(self._background.discard)

    async def _actions(self, member: AssistantMember) -> tuple[Action, ...]:
        """What the chat model may do for this person: reminders for everyone, the family's lists for family."""

        async def set_reminder(args: dict[str, Any]) -> str:
            request = str(args.get("request") or "").strip()
            if not request:
                return "Не сказано, о чём и когда напомнить."
            said = await self.reminders.create(member, f"напомни {request}")
            return f"Человеку показано: {said}"

        async def schedule_task(args: dict[str, Any]) -> str:
            request = str(args.get("request") or "").strip()
            if not request:
                return "Не сказано, что и когда делать."
            said = await self.reminders.create(member, f"напомни {request}", kind="do")
            return f"Человеку показано: {said}. В это время ты сама выполнишь задание и пришлёшь результат."

        async def move_reminder(args: dict[str, Any]) -> str:
            number = number_of(args)
            return (
                "Нет номера напоминания."
                if number is None
                else await self.reminders.move(member, number, str(args.get("when") or ""))
            )

        async def cancel_reminder(args: dict[str, Any]) -> str:
            number = number_of(args)
            return "Нет номера напоминания." if number is None else await self.reminders.cancel(member, number)

        runs = (set_reminder, schedule_task, move_reminder, cancel_reminder)
        actions = [
            Action(schema, run, REMINDERS_BLOCK, REMINDER_HINT)
            for schema, run in zip(REMINDER_TOOLS, runs, strict=True)
        ]
        if member.role in access.FAMILY_ROLES:
            try:
                family, personal = await self.lists.names(member)
            except Exception:
                logger.warning("could not load the family lists", exc_info=True)
                return tuple(actions)

            async def add_to_list(args: dict[str, Any]) -> str:
                items = args.get("items")
                if not isinstance(items, list):
                    return "Нет пунктов."
                result: str = await self.lists.add_from_chat(
                    member, str(args.get("list") or ""), [str(i) for i in items], args.get("personal") is True
                )
                return result

            names = {"names": quoted(family), "personal": quoted(personal)}
            actions.append(Action(LIST_TOOL, add_to_list, LISTS_BLOCK.format(**names), LISTS_HINT.format(**names)))
        return tuple(actions)

    async def _run_task(self, member: AssistantMember, task: str) -> None:
        """A task Luna was given for this time: she carries it out in the person's chat, with its history."""
        await self._chat(member, SCHEDULED_TASK.format(task=task), scheduled=True)

    async def _about(self, member: AssistantMember, text: str) -> str:
        """Luna's facts about herself, only for a question about her; a failure just leaves them out."""
        if not is_about_question(text):
            return ""
        owner = None
        if member.role != "guest":
            try:
                async with self._sessions() as db:
                    owner = await owner_name(db)
            except Exception:
                logger.warning("could not look up the owner", exc_info=True)
        facts: str = about_luna(member.role, owner)
        if member.role in access.FAMILY_ROLES:
            try:
                memories = await self.stories.memories()
            except Exception:
                logger.warning("could not load the cat stories", exc_info=True)
                memories = ""
            if memories:
                facts = STORY_MEMORIES.format(stories=memories) + facts
        return facts

    async def _waiting(self, member: AssistantMember, text: str, speak: bool, photo: bool) -> Waiting:
        """The cat's phrases for this answer; the family's own ones are never shown to guests."""
        kind = "math" if MATH_TEXT.search(text) else "photo" if photo else "voice" if speak else "chat"
        family: list[str] = []
        if member.role in access.FAMILY_ROLES:
            try:
                family = await self.phrases.family_phrases() + await self.stories.phrases()
            except Exception:
                logger.warning("could not load family phrases", exc_info=True)
        now = datetime.now(UTC) + timedelta(hours=settings.luna_utc_offset_hours)
        return Waiting(kind, now.replace(tzinfo=None), family)

    async def _react(self, chat_id: int, message_id: int | None, emoji: str | None) -> None:
        """A reaction on the person's message; None takes it away. Purely decorative, so failures are ignored."""
        if message_id is None:
            return
        reaction: list[ReactionTypeEmoji | ReactionTypeCustomEmoji | ReactionTypePaid] = (
            [ReactionTypeEmoji(emoji=emoji)] if emoji else []
        )
        try:
            await self._bot.set_message_reaction(chat_id, message_id, reaction)
        except (TelegramAPIError, ClientError, TimeoutError) as exc:
            logger.info("could not set a reaction: %s", type(exc).__name__)

    async def _planner(self, member: AssistantMember) -> str:
        """Timetable, tasks, calendar, reminders and the family's lists for the model; a failure only loses this
        context."""
        parts: list[str] = []
        for source in (self.school.planner_block, self.calendar.planner_text, self.reminders.planner_text):
            try:
                parts.append(await source(member.telegram_id))
            except Exception:
                logger.warning("could not load planner context", exc_info=True)
        if member.role in access.FAMILY_ROLES:
            try:
                parts.append(await self.lists.planner_text(member))
            except Exception:
                logger.warning("could not load the family lists", exc_info=True)
        return "\n\n".join(part for part in parts if part)

    async def _brief_extra(self, user_id: int, today: date) -> list[str]:
        lines: list[str] = []
        for source in (self.calendar.brief_lines, self.school.brief_lines):
            try:
                lines += await source(user_id, today)
            except Exception:
                logger.warning("could not load brief lines", exc_info=True)
        try:
            async with self._sessions() as db:
                member = await access.get_member(db, user_id)
            if member is not None:
                lines += await self.lists.brief_lines(member, today)
                lines += await self.stories.brief_lines(member, today)
        except Exception:
            logger.warning("could not load the lists or a cat story for the brief", exc_info=True)
        return lines

    async def _may_ask(self, member: AssistantMember) -> bool:
        """The rate limit and guest access, for requests that skip the chat."""
        if not self._allow_request(member.telegram_id):
            await self._send(member.telegram_id, TOO_FAST)
            return False
        if access.access_ended(member):
            await self._send(member.telegram_id, ACCESS_ENDED)
            return False
        return True

    async def _new_reminder(self, member: AssistantMember, text: str) -> None:
        if await self._may_ask(member):
            async with ChatActionSender.typing(chat_id=member.telegram_id, bot=self._bot):
                said = await self.reminders.create(member, text)
            await self._keep_turn(member, text, said)

    async def _keep_turn(self, member: AssistantMember, text: str, said: str) -> None:
        """A reminder or a list change done outside the chat still belongs to the conversation, so a follow-up
        such as "не в 8, а в 8:30" is understood."""
        user_id = member.telegram_id
        await self._conversation(member)  # loads the stored turns first, so they are not lost behind this one
        self._history[user_id].append({"role": "user", "content": text})
        self._history[user_id].append({"role": "assistant", "content": said})
        if self._memory:
            try:
                await self._memory.save_exchange(user_id, text, said, HISTORY_MESSAGES)
            except Exception:
                logger.warning("could not keep the turn", exc_info=True)

    async def _send_voice_reply(self, user_id: int, reply: str) -> bool:
        """Say the reply after showing it; the text is already there if this fails."""
        spoken = await self._speak(reply)
        if spoken is None:
            return False
        try:
            await self._bot.send_voice(user_id, BufferedInputFile(spoken.ogg, "luna.ogg"), duration=spoken.seconds)
        except TelegramAPIError as exc:
            logger.warning("could not send a voice reply: %s", type(exc).__name__)
            return False
        return True

    async def _say_last_reply(self, member: AssistantMember) -> None:
        """Answer "say it aloud" by voicing the last answer, with no model that could refuse."""
        user_id = member.telegram_id
        if not self._allow_request(user_id):
            await self._send(user_id, TOO_FAST)
            return
        if access.access_ended(member):
            await self._send(user_id, ACCESS_ENDED)
            return
        history = without_voice_refusals(await self._conversation(member))
        last = next((turn["content"] for turn in reversed(history) if turn["role"] == "assistant"), None)
        if last is None:
            await self._send(user_id, NOTHING_TO_SAY)
        elif not await self._send_voice_reply(user_id, last):
            await self._send(user_id, CANNOT_SAY)

    async def _conversation(self, member: AssistantMember) -> list[dict[str, str]]:
        """After a restart the last turns come back from the database once."""
        user_id = member.telegram_id
        if self._memory and user_id not in self._history_loaded:
            self._history_loaded.add(user_id)
            try:
                self._history[user_id].extend(await self._memory.load_history(user_id, HISTORY_MESSAGES))
            except Exception:
                logger.warning("could not load the conversation", exc_info=True)
        return list(self._history[user_id])

    async def _save_family_note(self, member: AssistantMember, note: str) -> None:
        if self._memory is None:
            await self._send(member.telegram_id, FAMILY_REPLIES[FAMILY_FAILED])
            return
        outcome = await self._memory.family.add(member.telegram_id, member.name, note)
        await self._send(member.telegram_id, FAMILY_REPLIES[outcome])

    @staticmethod
    def _may_delete_family_note(member: AssistantMember, author_id: int) -> bool:
        return member.role in access.ADULT_ROLES or member.telegram_id == author_id

    async def _family_view(self, member: AssistantMember, page: int) -> tuple[str, InlineKeyboardMarkup | None]:
        if self._memory is None:
            return "Память сейчас недоступна.", None
        facts = await self._memory.family.list_facts()
        last_page = max(len(facts) - 1, 0) // MEMORY_PAGE_SIZE
        page = max(0, min(page, last_page))
        shown = facts[page * MEMORY_PAGE_SIZE : (page + 1) * MEMORY_PAGE_SIZE]
        first = page * MEMORY_PAGE_SIZE + 1
        lines = [f"{first + i}. {fact.content} ({fact.author_name})" for i, fact in enumerate(shown)]
        text = (
            f"🏠 Общая память семьи ({len(facts)}):\n\n" + "\n".join(lines) if facts else "🏠 Общая память пока пуста."
        )
        delete_buttons = [
            InlineKeyboardButton(text=f"🗑 {first + i}", callback_data=f"famd:{fact.id.hex}")
            for i, fact in enumerate(shown)
            if self._may_delete_family_note(member, fact.author_id)
        ]
        rows = [delete_buttons[i : i + 4] for i in range(0, len(delete_buttons), 4)]
        navigation = []
        if page > 0:
            navigation.append(InlineKeyboardButton(text="←", callback_data=f"fam:{page - 1}"))
        if page < last_page:
            navigation.append(InlineKeyboardButton(text="→", callback_data=f"fam:{page + 1}"))
        if navigation:
            rows.append(navigation)
        rows.append([InlineKeyboardButton(text="➕ Добавить", callback_data="fama")])
        rows.append([InlineKeyboardButton(text="← Назад", callback_data="mem:0")])
        return text, InlineKeyboardMarkup(inline_keyboard=rows)

    async def _confirm_family_delete(
        self, member: AssistantMember, memory_hex: str
    ) -> tuple[str, InlineKeyboardMarkup | None]:
        fact = await self._memory.family.get(uuid.UUID(hex=memory_hex)) if self._memory else None
        if fact is None or not self._may_delete_family_note(member, fact.author_id):
            return await self._family_view(member, 0)
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Да, удалить", callback_data=f"famy:{memory_hex}", style="danger")],
                [InlineKeyboardButton(text="← Назад", callback_data="fam:0")],
            ]
        )
        return f"Удалить для всей семьи?\n\n{fact.content}", markup

    async def _forget_family_fact(self, callback: CallbackQuery, member: AssistantMember, memory_hex: str) -> None:
        """The right to delete is checked again here, since callback data can outlive a role change."""
        memory_id = uuid.UUID(hex=memory_hex)
        fact = await self._memory.family.get(memory_id) if self._memory else None
        if self._memory is not None and fact is not None and self._may_delete_family_note(member, fact.author_id):
            await self._memory.family.forget(memory_id)
        await self._edit(callback, *await self._family_view(member, 0))

    def _with_family_button(
        self, member: AssistantMember, view: tuple[str, InlineKeyboardMarkup | None]
    ) -> tuple[str, InlineKeyboardMarkup | None]:
        if member.role not in access.FAMILY_ROLES:
            return view
        text, markup = view
        rows = [*(markup.inline_keyboard if markup else [])]
        rows.append(
            [
                InlineKeyboardButton(text="🏠 Общая память", callback_data="fam:0"),
                InlineKeyboardButton(text=BTN_STORY, callback_data="csi"),
            ]
        )
        return text, InlineKeyboardMarkup(inline_keyboard=rows)

    async def _memory_view(self, member: AssistantMember, page: int) -> tuple[str, InlineKeyboardMarkup | None]:
        return self._with_family_button(member, await self._personal_view(member.telegram_id, page))

    async def _personal_view(self, user_id: int, page: int) -> tuple[str, InlineKeyboardMarkup | None]:
        if self._memory is None:
            return "Память сейчас недоступна.", None
        facts = await self._memory.list_facts(user_id)
        if not facts:
            return "Пока ничего о тебе не запомнила. Расскажи о себе, и я учту это в разговорах.", None
        last_page = (len(facts) - 1) // MEMORY_PAGE_SIZE
        page = max(0, min(page, last_page))
        shown = facts[page * MEMORY_PAGE_SIZE : (page + 1) * MEMORY_PAGE_SIZE]
        first = page * MEMORY_PAGE_SIZE + 1
        lines = [f"{first + i}. {fact.content}" for i, fact in enumerate(shown)]
        text = f"👤 Что я о тебе помню ({len(facts)}):\n\n" + "\n".join(lines) + "\n\nНажми номер, чтобы удалить."
        delete_buttons = [
            InlineKeyboardButton(text=f"🗑 {first + i}", callback_data=f"memd:{fact.id.hex}:{page}")
            for i, fact in enumerate(shown)
        ]
        rows = [delete_buttons[i : i + 4] for i in range(0, len(delete_buttons), 4)]
        navigation = []
        if page > 0:
            navigation.append(InlineKeyboardButton(text="←", callback_data=f"mem:{page - 1}"))
        if page < last_page:
            navigation.append(InlineKeyboardButton(text="→", callback_data=f"mem:{page + 1}"))
        if navigation:
            rows.append(navigation)
        rows.append([InlineKeyboardButton(text="Очистить всё", callback_data="memc", style="danger")])
        return text, InlineKeyboardMarkup(inline_keyboard=rows)

    async def _forget_fact(self, callback: CallbackQuery, member: AssistantMember, memory_hex: str, page: int) -> None:
        if self._memory is not None:
            await self._memory.forget(member.telegram_id, uuid.UUID(hex=memory_hex))
        await self._edit(callback, *await self._memory_view(member, page))

    @staticmethod
    def _confirm_forget_all() -> tuple[str, InlineKeyboardMarkup]:
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Да, забыть всё", callback_data="memy", style="danger")],
                [InlineKeyboardButton(text="← Назад", callback_data="mem:0")],
            ]
        )
        return "Забыть всё, что я о тебе знаю, и наш последний разговор? Отменить будет нельзя.", markup

    async def _forget_everything(self, callback: CallbackQuery, user_id: int) -> None:
        if self._memory is not None:
            await self._memory.forget_all(user_id)
        self._history[user_id].clear()
        await self._edit(callback, "Готово, всё забыла.", None)

    async def _alert_parents(self, member: AssistantMember, text: str) -> None:
        async with self._sessions() as db:
            parents = await access.adult_chat_ids(db)
        for parent_id in parents:
            try:
                await self._send(parent_id, f"🚨 {member.name} написал(а) Luna, стоит поговорить:\n\n{text[:1000]}")
            except TelegramAPIError as exc:
                logger.warning("could not alert a parent: %s", type(exc).__name__)


async def report_time_decisions(bot: Bot, pi: PiClient, session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Tell children the outcome of requests that parents have resolved on the Pi."""
    async with session_factory() as db:
        requests = await access.pending_time_requests(db)
    now = datetime.now(UTC)
    for request in requests:
        try:
            status = await pi.time_request_status(request.request_id)
        except PiNotFoundError:
            status = {"status": "denied", "minutes": 0}
        except PiUnavailableError:
            continue
        if status["status"] == "pending":
            if now - request.created_at > TIME_REQUEST_TTL:
                async with session_factory() as db:
                    await access.finish_time_request(db, request.request_id)
            continue
        if status["status"] != "approved":
            text = "Родители пока не добавили время. Поговори с ними 💬"
        elif status.get("category") == "pause":
            text = f"🎉 Родители открыли интернет на {status['minutes']} минут!"
        else:
            text = f"🎉 Родители добавили {status['minutes']} минут!"
        await send_long(bot, request.telegram_id, text)
        async with session_factory() as db:
            await access.finish_time_request(db, request.request_id)


async def watch_time_requests(bot: Bot, pi: PiClient, session_factory: async_sessionmaker[AsyncSession]) -> None:
    while True:
        try:
            await report_time_decisions(bot, pi, session_factory)
        except (TelegramAPIError, OSError) as exc:
            logger.warning("time decision report failed: %s", type(exc).__name__)
        await asyncio.sleep(TIME_POLL_SECONDS)


def build_router(luna: LunaBot) -> Router:
    router = Router()
    router.message.register(luna.on_message, F.chat.type == "private")
    router.callback_query.register(luna.on_callback)
    return router
