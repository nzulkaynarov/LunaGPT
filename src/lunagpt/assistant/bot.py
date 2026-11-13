"""Telegram message handling for Luna AI Assistant."""

import asyncio
import html
import logging
import re
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    Document,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
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

from lunagpt.assistant import access, updates, usage
from lunagpt.assistant.calendar_desk import CALENDAR_PREFIXES, CalendarDesk, is_calendar_request
from lunagpt.assistant.chat_context import ChatContext
from lunagpt.assistant.dialogue import Dialogue
from lunagpt.assistant.gcal import GoogleCalendar
from lunagpt.assistant.internet_desk import BTN_ASK_TIME, BTN_QUIZ, INTERNET_PREFIXES, InternetDesk
from lunagpt.assistant.list_desk import LIST_PREFIXES, ListDesk
from lunagpt.assistant.lists import is_list_request
from lunagpt.assistant.llm import SCHEDULED_TASK, QuizQuestion, ReplyOptions, transcribe_voice
from lunagpt.assistant.memories import Memory
from lunagpt.assistant.memory_desk import (
    BTN_STORY,
    FAMILY_MEMORY_PREFIXES,
    FAMILY_PROMPT,
    MEMORY_PREFIXES,
    MemoryDesk,
)
from lunagpt.assistant.menu import (
    BACK_AGAIN,
    BTN_DAY,
    BTN_FAMILY,
    BTN_HELP,
    BTN_INTERNET,
    BTN_INVITE,
    BTN_LISTS,
    BTN_MEMORY,
    BTN_PEOPLE,
    BTN_PHRASE,
    FAMILY_MENU,
    HELP_TEXT,
    MENU_FEATURES,
    OLD_BUTTONS,
    OLD_SHARED,
    SHARED_MOVED,
    WELCOME,
    family_keyboard,
    main_keyboard,
    menu_layout,
)
from lunagpt.assistant.people import PEOPLE_PREFIXES, PeopleDesk
from lunagpt.assistant.phrases import PHRASE_PREFIXES, PhraseDesk
from lunagpt.assistant.pi_client import PiClient
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
from lunagpt.assistant.waiting import cat_is_thinking
from lunagpt.config import settings
from lunagpt.models import AssistantMember

logger = logging.getLogger("lunagpt.assistant.bot")

AnswerFn = Callable[[str, int, list[dict[str, str]], str, list[str], ReplyOptions], Awaitable[str]]
QuizFn = Callable[[int, int | None], Awaitable[QuizQuestion | None]]
TranscribeFn = Callable[[str, int, bytes, Callable[[float], None] | None, bool], Awaitable[str | None]]
SpeakFn = Callable[[str], Awaitable[SpokenReply | None]]

ACCESS_ENDED = "Срок доступа закончился. Попроси взрослого из семьи его продлить."
TOO_FAST = "🐾 Помедленнее, я не успеваю за мышкой! Подожди минутку."
TEXT_ONLY = "Пока я понимаю текст, голосовые, кружки и фото."
PHOTO_DEFAULT = "Что на фото? Если там задание, помоги с ним."
PHOTO_FAILED = "Не получилось открыть фото. Пришли его ещё раз."
PHOTO_TOO_BIG = "Слишком большой файл. Пришли фото обычным способом, не файлом."
MAX_PHOTO_BYTES = 10 * 1024 * 1024
PHOTO_TYPES = ("image/jpeg", "image/png", "image/webp")
THINKING_REACTION = "👀"
DONE_REACTION = "🤗"  # Telegram allows only its own reaction set, which has no cat
NO_VIDEO = "Видео я не смотрю. Пришли голосовое или кружок, или напиши текстом."
MAX_VOICE_SECONDS = 120
VOICE_TOO_LONG = "Слишком длинное голосовое. Запиши покороче, до двух минут."
VOICE_FAILED = "Не получилось разобрать голосовое. Попробуй ещё раз или напиши текстом."
NOTHING_TO_SAY = "Спроси что-нибудь, и я отвечу голосом."
CANNOT_SAY = "Этот ответ вслух не прочитать: он длинный или со ссылками. Спроси покороче."
VOICE_NOT_HEARD = "Не расслышала слов в этом голосовом. Попробуй ещё раз."
UPDATE_CHECK_SECONDS = 600
RESTARTED_TEXT = updates.RESTARTED
FAMILY_REQUEST = re.compile(
    r"^\s*(?:запомни|запиши|сохрани)\s+(?:для\s+(?:всей\s+)?(?:семьи|всех)|в\s+(?:семейн\w+|общ\w+)\s+памят\w+)"
    r"[\s:,—-]*(?:что\s+)?(?P<fact>\S.*)$",
    re.IGNORECASE | re.DOTALL,
)
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
        self._pi = pi
        self._username = bot_username
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
        self.dialogue = Dialogue(lambda: self._memory)
        self.memories = MemoryDesk(bot, memory, self.dialogue.forget)
        self.internet = InternetDesk(bot, session_factory, pi, make_quiz, self._today)
        self.context: ChatContext = ChatContext(
            session_factory,
            reminders=self.reminders,
            lists=self.lists,
            school=self.school,
            calendar=self.calendar,
            stories=self.stories,
            phrases=self.phrases,
        )
        self._background: set[asyncio.Task[None]] = set()
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    @property
    def _memory(self) -> Memory | None:
        return self.memories.memory

    @_memory.setter
    def _memory(self, value: Memory | None) -> None:
        self.memories.memory = value

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
        if picture is not None and await self._raised_alarm(member, (message.caption or "").strip()):
            return
        if picture is not None and member.role in access.FAMILY_ROLES and self.stories.is_reply(message):
            await self.stories.tell(member, (message.caption or "").strip(), picture.file_id)
            await self._drop_prompt(message)
            return
        if picture is not None:
            await self._count(user_id, "photo")
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
        # Before any route: a child in trouble may type it as a reminder, a list or homework.
        if await self._raised_alarm(member, text):
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
        if member.role in access.FAMILY_ROLES and is_list_request(text) and not self.dialogue.over_rate_limit(user_id):
            async with ChatActionSender.typing(chat_id=user_id, bot=self._bot):
                said = await self.lists.request(member, text)
            if said is not None:
                await self._count(user_id, "lists")
                self.dialogue.allow_request(user_id)  # counts like a chat turn; anything else goes on to the chat
                await self._keep_turn(member, text, said)
                return
        if spoken_note is None and is_bare_voice_request(text):
            await self._say_last_reply(member)
            return
        if asks_for_voice(text):
            await self._chat(member, without_voice_words(text), speak=True)
            return
        await self._chat(member, text, speak=spoken_note is not None, message_id=message.message_id)

    async def _count(self, member_id: int, feature: str, cost: float = 0.0) -> None:
        await usage.record(self._sessions, member_id, feature, self._today(), cost)

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
            await self._send(member.telegram_id, *await self.memories.view(member, 0))
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
            await self.internet.view(member)
        elif text == BTN_ASK_TIME and kid:
            await self.internet.ask(member, "")
        elif text == BTN_QUIZ and kid:
            await self.internet.start_quiz(member)
        elif text == BTN_STORY and family:
            await self.stories.view(member)
        elif text == BTN_PHRASE and kid:
            await self.phrases.ask(member)
        else:
            return False
        feature = MENU_FEATURES.get(text)
        if feature is not None:
            await self._count(member.telegram_id, feature)
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
        if access.access_ended(member) and prefix != updates.UPDATE_PREFIX:
            await callback.answer(ACCESS_ENDED, show_alert=True)
            return
        await callback.answer()
        feature = usage.feature_of_prefix(prefix)
        if feature is not None:
            await self._count(user_id, feature)
        args = data.split(":")[1:]
        if prefix == updates.UPDATE_PREFIX:
            await self._restart(callback, member)
        elif prefix == "fhp":
            await self._edit(callback, *await self._people.summary())
        elif prefix == "fhi":
            await remove_menu(callback)
            await self._people.send_invite(user_id)
        elif prefix == PHRASE_ASK:
            await self.phrases.ask(member)
            await remove_menu(callback)
        elif prefix in ("sh", "shs", "shl"):  # buttons of a device list sent before the move to HomeSec
            await self._edit(callback, SHARED_MOVED, None)
        elif prefix in MEMORY_PREFIXES or prefix in FAMILY_MEMORY_PREFIXES:
            await self.memories.handle(callback, member, prefix, args)
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
        elif prefix in INTERNET_PREFIXES:
            await self.internet.handle(callback, member, prefix, args)

    @staticmethod
    def _callback_allowed(role: str, prefix: str) -> bool:
        if prefix == updates.UPDATE_PREFIX:
            return role in access.ROLES
        if prefix in INTERNET_PREFIXES or prefix == PHRASE_ASK:
            return role == "kid"
        if prefix in ("fhp", "fhi"):
            return role in access.ADULT_ROLES
        if prefix in MEMORY_PREFIXES or prefix in REMINDER_PREFIXES | SCHOOL_PREFIXES | CALENDAR_PREFIXES:
            return role in access.ROLES
        if prefix in FAMILY_MEMORY_PREFIXES:
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
            question = caption or self.dialogue.recent_question(user_id) or PHOTO_DEFAULT
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
        if self.dialogue.over_rate_limit(user_id):
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
                        costs.append,
                        access.money_limit_reached(member, today),
                    )
                    if audio is not None
                    else None
                )
        except (TelegramAPIError, ClientError, TimeoutError) as exc:
            logger.warning("could not download a voice note: %s", type(exc).__name__)
            words = None
        if any(costs) and member.role == "guest":
            async with self._sessions() as db:
                await access.record_spend(db, user_id, sum(costs), today)
        await self._count(user_id, "voice", sum(costs))
        if not words:
            await self._send(user_id, VOICE_FAILED if words is None else VOICE_NOT_HEARD)
            return None
        await self._send(user_id, f"🗣 «{words}»")  # a misheard word is visible before the answer
        return words

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
        if not scheduled and await self._raised_alarm(member, text):
            return
        if not scheduled and not self.dialogue.allow_request(user_id):
            await self._send(user_id, TOO_FAST)
            return
        if member.role in access.FAMILY_ROLES and image is None and not scheduled:
            family_note = family_note or family_request(text)
        if family_note is not None:
            await self.memories.save_family_note(member, family_note)
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
            self.dialogue.note_question(user_id, text)
            recent = self.dialogue.recent_photo(user_id)
            if recent is not None:
                image, image_type = recent
                earlier_photo = True
        else:
            self.dialogue.keep_photo(user_id, image, image_type)
        waiting = await self.context.waiting(member, text, speak, image is not None)
        await self._react(user_id, message_id, THINKING_REACTION)
        async with self._locks[user_id]:
            async with (
                ChatActionSender.typing(chat_id=user_id, bot=self._bot),  # repeats while the model works
                cat_is_thinking(self._bot, user_id, waiting),
            ):
                history = without_voice_refusals(await self.dialogue.turns(user_id))
                facts = await self._memory.recall(user_id, member.role, text) if self._memory else []
                options = ReplyOptions(
                    age=access.age_of(current, today),
                    local_only=access.money_limit_reached(current, today),
                    on_cost=costs.append,
                    family_facts=await self._memory.family.recall(current.role, text) if self._memory else (),
                    spoken=speak,
                    image=image,
                    image_type=image_type,
                    earlier_photo=earlier_photo,
                    planner=await self.context.planner(current),
                    on_search=waiting.searching,
                    about=await self.context.about(current, text),
                    actions=await self.context.actions(current),
                )
                reply = await self._answer(member.role, user_id, history, text, facts, options)
            # Later turns keep only a mark of the photo: its bytes are never stored or sent again.
            said = f"[фото] {text}" if image is not None and not earlier_photo else text
            await self.dialogue.add(user_id, said, reply)
        if any(costs) and current.role == "guest":
            async with self._sessions() as db:
                await access.record_spend(db, user_id, sum(costs), today)
        await self._count(user_id, "chat", sum(costs))
        await send_model_reply(self._bot, user_id, reply)
        await self._react(user_id, message_id, DONE_REACTION if member.role == "kid" else None)
        if speak:
            await self._send_voice_reply(user_id, reply)
        if self._memory and text != PHOTO_DEFAULT and not scheduled:
            task = asyncio.create_task(self._memory.remember(user_id, member.role, text))
            self._background.add(task)  # the event loop keeps only weak references to tasks
            task.add_done_callback(self._background.discard)

    async def _brief_extra(self, user_id: int, today: date) -> list[str]:
        lines: list[str] = await self.context.brief_extra(user_id, today)
        return lines

    async def _run_task(self, member: AssistantMember, task: str) -> None:
        """A task Luna was given for this time: she carries it out in the person's chat, with its history."""
        await self._chat(member, SCHEDULED_TASK.format(task=task), scheduled=True)

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

    async def _may_ask(self, member: AssistantMember) -> bool:
        """The rate limit and guest access, for requests that skip the chat."""
        if not self.dialogue.allow_request(member.telegram_id):
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
            await self._count(member.telegram_id, "reminders")
            await self._keep_turn(member, text, said)

    async def _keep_turn(self, member: AssistantMember, text: str, said: str) -> None:
        """A reminder or a list change done outside the chat still belongs to the conversation, so a follow-up
        such as "не в 8, а в 8:30" is understood."""
        user_id = member.telegram_id
        await self.dialogue.turns(user_id)  # loads the stored turns first, so they are not lost behind this one
        try:
            await self.dialogue.add(user_id, text, said)
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
        if not self.dialogue.allow_request(user_id):
            await self._send(user_id, TOO_FAST)
            return
        if access.access_ended(member):
            await self._send(user_id, ACCESS_ENDED)
            return
        history = without_voice_refusals(await self.dialogue.turns(user_id))
        last = next((turn["content"] for turn in reversed(history) if turn["role"] == "assistant"), None)
        if last is None:
            await self._send(user_id, NOTHING_TO_SAY)
        elif not await self._send_voice_reply(user_id, last):
            await self._send(user_id, CANNOT_SAY)

    async def _raised_alarm(self, member: AssistantMember, text: str) -> bool:
        """A child's words that need an adult: tell the parents and answer gently; True when it happened."""
        if member.role != "kid" or not text or not needs_adult_attention(text):
            return False
        await self._alert_parents(member, text)
        await self._send(member.telegram_id, SAFETY_REPLY)
        return True

    async def _alert_parents(self, member: AssistantMember, text: str) -> None:
        async with self._sessions() as db:
            parents = await access.adult_chat_ids(db)
        for parent_id in parents:
            try:
                await self._send(parent_id, f"🚨 {member.name} написал(а) Luna, стоит поговорить:\n\n{text[:1000]}")
            except TelegramAPIError as exc:
                logger.warning("could not alert a parent: %s", type(exc).__name__)


HANDLER_FAILED = "😿 Не получилось. Попробуй ещё раз чуть позже."


async def on_error(event: ErrorEvent) -> bool:
    """Log a failed update and tell the person, instead of leaving them with silence."""
    logger.error("update failed: %s", type(event.exception).__name__, exc_info=event.exception)
    update = event.update
    try:
        if update.callback_query is not None:
            await update.callback_query.answer(HANDLER_FAILED, show_alert=True)
        elif update.message is not None and update.message.chat.type == "private":
            await update.message.answer(HANDLER_FAILED)
    except TelegramAPIError as exc:
        logger.warning("could not report a failed update: %s", type(exc).__name__)
    return True


def build_router(luna: LunaBot) -> Router:
    router = Router()
    router.message.register(luna.on_message, F.chat.type == "private")
    router.callback_query.register(luna.on_callback)
    router.errors.register(on_error)
    return router
