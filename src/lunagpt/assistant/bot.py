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
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    Voice,
)
from aiogram.utils.chat_action import ChatActionSender
from aiohttp import ClientError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lunagpt.assistant import access
from lunagpt.assistant.llm import QuizQuestion, ReplyOptions, transcribe_voice
from lunagpt.assistant.memories import FAMILY_DUPLICATE, FAMILY_FAILED, FAMILY_FULL, FAMILY_SAVED, Memory
from lunagpt.assistant.people import PEOPLE_PREFIXES, PeopleDesk
from lunagpt.assistant.pi_client import PiClient, PiConflictError, PiNotFoundError, PiUnavailableError
from lunagpt.assistant.speech import SpokenReply, asks_for_voice, speak_reply
from lunagpt.assistant.telegram import is_reply_to_prompt, send_long, send_model_reply
from lunagpt.assistant.waiting import cat_is_thinking
from lunagpt.config import settings
from lunagpt.models import AssistantMember

logger = logging.getLogger("lunagpt.assistant.bot")

AnswerFn = Callable[[str, int, list[dict[str, str]], str, list[str], ReplyOptions], Awaitable[str]]
QuizFn = Callable[[int, int | None], Awaitable[QuizQuestion | None]]
TranscribeFn = Callable[[str, int, bytes, Callable[[float], None] | None, bool], Awaitable[str | None]]
SpeakFn = Callable[[str], Awaitable[SpokenReply | None]]

BTN_MY_TIME = "⏳ Моё время"
BTN_ASK_TIME = "🙏 Попросить время"
BTN_QUIZ = "🧠 Викторина +15 мин"
BTN_SHARED = "📺 Общие устройства"
BTN_INVITE = "➕ Пригласить"
BTN_PEOPLE = "👥 Люди"
BTN_MEMORY = "🧠 Что ты помнишь"
BTN_HELP = "❓ Помощь"
MENU_BUTTONS = {
    BTN_MY_TIME,
    BTN_ASK_TIME,
    BTN_QUIZ,
    BTN_SHARED,
    BTN_INVITE,
    BTN_PEOPLE,
    BTN_MEMORY,
    BTN_HELP,
}
MEMORY_PAGE_SIZE = 8
SHARED_LABELS = {"kid": "👶 Детский режим", "off": "⛔ Выключено", "adult": "🍿 Для взрослых"}
TIME_POLL_SECONDS = 30
TIME_REQUEST_TTL = timedelta(hours=24)
PI_OFFLINE = "Сейчас домашняя сеть не отвечает, попробуй чуть позже."
NO_DEVICE = "Я не нашла твоё устройство в домашней сети. Попроси родителей привязать его."

HISTORY_MESSAGES = 8
RATE_LIMIT = 10
RATE_WINDOW_SECONDS = 60
ACCESS_ENDED = "Срок доступа закончился. Попроси взрослого из семьи его продлить."
TOO_FAST = "Слишком много сообщений подряд. Подожди минуту 🙂"
TEXT_ONLY = "Пока я понимаю текст и голосовые сообщения."
MAX_VOICE_SECONDS = 120
VOICE_TOO_LONG = "Слишком длинное голосовое. Запиши покороче, до двух минут."
VOICE_FAILED = "Не получилось разобрать голосовое. Попробуй ещё раз или напиши текстом."
VOICE_NOT_HEARD = "Не расслышала слов в этом голосовом. Попробуй ещё раз."
MEMORY_NOTICE = (
    " Запоминаю важное о тебе, посмотреть и удалить можно на кнопке «Что ты помнишь». Другие люди этого не видят."
)
FAMILY_NOTICE = " Чтобы запомнить для всех в семье, напиши: «запомни для семьи, что…»."
ASK_ANYTHING = "Просто напиши или скажи голосом, что нужно: вопрос, идея, перевод, свежие новости."
ADULT_HELP = (
    f"{ASK_ANYTHING}\n\n"
    f"{BTN_PEOPLE} — семья, гости и заявки\n"
    f"{BTN_SHARED} — телевизор и другое домашнее\n"
    f"{BTN_INVITE} — отправить приглашение\n"
    f"{BTN_MEMORY} — личная и общая память\n\n" + MEMORY_NOTICE.strip() + FAMILY_NOTICE
)
HELP_TEXT = {
    "owner": ADULT_HELP,
    "adult": ADULT_HELP,
    "kid": (
        "Спрашивай что угодно: уроки, игры, код, идеи, свежие новости.\n\n"
        f"{BTN_MY_TIME} — сколько осталось интернета\n"
        f"{BTN_ASK_TIME} — написать родителям\n"
        f"{BTN_QUIZ} — ответить и заработать время\n"
        f"{BTN_MEMORY} — личная и общая память\n\n" + MEMORY_NOTICE.strip() + FAMILY_NOTICE
    ),
    "guest": f"{ASK_ANYTHING}\n\n{BTN_MEMORY} — что я о тебе помню\n\n" + MEMORY_NOTICE.strip(),
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


def needs_adult_attention(text: str) -> bool:
    return bool(SAFETY_PATTERN.search(text))


def main_keyboard(role: str) -> ReplyKeyboardMarkup:
    """The persistent menu; server-side checks still decide what each role may do."""
    adults = [[BTN_SHARED, BTN_PEOPLE], [BTN_INVITE, BTN_MEMORY], [BTN_HELP]]
    layouts = {
        "owner": adults,
        "adult": adults,
        "kid": [[BTN_MY_TIME, BTN_ASK_TIME], [BTN_QUIZ, BTN_MEMORY], [BTN_HELP]],
        "guest": [[BTN_MEMORY, BTN_HELP]],
    }
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=text) for text in row] for row in layouts[role]],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Напиши Luna…",
    )


def shared_actions_keyboard(device_id: int) -> InlineKeyboardMarkup:
    action = f"shs:{device_id}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⛔ Выключить", callback_data=f"{action}:off", style="danger"),
                InlineKeyboardButton(text="👶 Детский режим", callback_data=f"{action}:kid"),
            ],
            [
                InlineKeyboardButton(text="🍿 Взрослым на 3 часа", callback_data=f"{action}:3", style="success"),
                InlineKeyboardButton(text="🌙 Взрослым до утра", callback_data=f"{action}:m", style="success"),
            ],
            [InlineKeyboardButton(text="← Все устройства", callback_data="shl")],
        ]
    )


def shared_status_text(device: dict[str, Any]) -> str:
    until = f" до {device['until'][11:16]}" if device.get("until") else ""
    return f"{device['name']}\nСейчас: {SHARED_LABELS[device['state']]}{until}"


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
        self._history_loaded: set[int] = set()
        self._background: set[asyncio.Task[None]] = set()
        self._history: dict[int, deque[dict[str, str]]] = defaultdict(lambda: deque(maxlen=HISTORY_MESSAGES))
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._request_times: dict[int, deque[float]] = defaultdict(lambda: deque(maxlen=RATE_LIMIT))
        self._quizzes: dict[int, QuizQuestion] = {}

    async def _send(
        self,
        chat_id: int,
        text: str,
        reply_markup: ReplyKeyboardMarkup | InlineKeyboardMarkup | None = None,
    ) -> None:
        await send_long(self._bot, chat_id, text, reply_markup)

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
            return
        if member.role in access.ADULT_ROLES and self._people.is_search_reply(message):
            await self._people.search(user_id, message)
            return
        if not text and message.voice is not None:
            text = await self._voice_text(member, message.voice) or ""
            if not text:
                return
        if not text:
            await self._send(user_id, TEXT_ONLY)
            return
        if member.role in access.FAMILY_ROLES and is_reply_to_prompt(message, FAMILY_PROMPT):
            await self._chat(member, text, family_note=text)
            return
        if await self._menu_action(member, text):
            return
        if text.startswith("/start"):
            await self._send(user_id, BACK_AGAIN.format(name=member.name), main_keyboard(member.role))
            return
        if text.startswith("/"):
            await self._send(user_id, HELP_TEXT[member.role], main_keyboard(member.role))
            return
        await self._chat(member, text, speak=message.voice is not None or asks_for_voice(text))

    async def _menu_action(self, member: AssistantMember, text: str) -> bool:
        """Run the pressed menu button if this role is allowed to use it."""
        adult = member.role in access.ADULT_ROLES
        if text == BTN_HELP:
            await self._send(member.telegram_id, HELP_TEXT[member.role], main_keyboard(member.role))
        elif text == BTN_PEOPLE and adult:
            await self._people.send_summary(member.telegram_id)
        elif text == BTN_INVITE and adult:
            await self._people.send_invite(member.telegram_id)
        elif text == BTN_SHARED and adult:
            await self._send_shared_list(member)
        elif text == BTN_MEMORY:
            await self._send(member.telegram_id, *await self._memory_view(member, 0))
        elif member.role == "kid" and text == BTN_MY_TIME:
            await self._my_time(member)
        elif member.role == "kid" and text == BTN_ASK_TIME:
            await self._ask_time(member, "")
        elif member.role == "kid" and text == BTN_QUIZ:
            await self._start_quiz(member)
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
        if prefix == "quiz":
            await self._answer_quiz(member, int(args[0]))
        elif prefix == "shl":
            await self._edit_shared_list(callback)
        elif prefix == "sh":
            await self._show_shared_device(callback, int(args[0]))
        elif prefix == "shs":
            await self._change_shared_device(callback, int(args[0]), args[1])
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
        elif prefix == "famd":
            await self._edit(callback, *await self._confirm_family_delete(member, args[0]))
        elif prefix == "famy":
            await self._forget_family_fact(callback, member, args[0])
        elif prefix in PEOPLE_PREFIXES:
            await self._people.handle(callback, member, prefix, args)

    @staticmethod
    def _callback_allowed(role: str, prefix: str) -> bool:
        if prefix == "quiz":
            return role == "kid"
        if prefix in ("mem", "memd", "memc", "memy"):
            return role in access.ROLES
        if prefix in ("fam", "fama", "famd", "famy"):
            return role in access.FAMILY_ROLES
        if prefix in PEOPLE_PREFIXES or prefix in ("sh", "shs", "shl"):
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

    async def _shared_list_view(self) -> tuple[str, InlineKeyboardMarkup | None]:
        try:
            devices = await self._pi.shared_devices()
        except (PiNotFoundError, PiUnavailableError):
            return PI_OFFLINE, None
        if not devices:
            return "Общих устройств пока нет.", None
        rows = [
            [
                InlineKeyboardButton(
                    text=f"{SHARED_LABELS[d['state']].split()[0]} {d['name']}"[:60], callback_data=f"sh:{d['id']}"
                )
            ]
            for d in devices
        ]
        return "Выбери устройство:", InlineKeyboardMarkup(inline_keyboard=rows)

    async def _send_shared_list(self, member: AssistantMember) -> None:
        text, markup = await self._shared_list_view()
        await self._send(member.telegram_id, text, markup)

    async def _edit_shared_list(self, callback: CallbackQuery) -> None:
        text, markup = await self._shared_list_view()
        await self._edit(callback, text, markup)

    async def _show_shared_device(self, callback: CallbackQuery, device_id: int) -> None:
        try:
            device = next((d for d in await self._pi.shared_devices() if int(d["id"]) == device_id), None)
        except (PiNotFoundError, PiUnavailableError):
            await self._edit(callback, PI_OFFLINE, None)
            return
        if device is None:
            await self._edit(callback, "Устройство не найдено.", None)
            return
        await self._edit(callback, shared_status_text(device), shared_actions_keyboard(device_id))

    async def _change_shared_device(self, callback: CallbackQuery, device_id: int, choice: str) -> None:
        try:
            device = await self._pi.set_shared_state(device_id, choice)
        except (PiNotFoundError, PiUnavailableError):
            await self._edit(callback, PI_OFFLINE, None)
            return
        await self._edit(callback, shared_status_text(device), shared_actions_keyboard(device_id))

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
            rows = await self._pi.quota(member.name)
        except PiNotFoundError:
            await self._send(member.telegram_id, NO_DEVICE)
            return
        except PiUnavailableError:
            await self._send(member.telegram_id, PI_OFFLINE)
            return
        if not rows:
            await self._send(member.telegram_id, "Лимитов на сегодня нет.")
            return
        lines = [f"{r['device']}, {r['category_label']}: {r['used_minutes']} из {r['limit_minutes']} мин" for r in rows]
        await self._send(member.telegram_id, "\n".join(lines))

    async def _ask_time(self, member: AssistantMember, reason: str) -> None:
        user_id = member.telegram_id
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
        text = "✅ Запрос отправлен родителям. Сообщу, когда они ответят."
        try:
            if await self._pi.quiz_available(member.name):
                text += f"\n\nПока ждёшь, можно заработать +15 минут: нажми «{BTN_QUIZ}»."
        except (PiNotFoundError, PiUnavailableError):
            pass
        await self._send(user_id, text)

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
        await self._send(user_id, f"🧠 {quiz.topic}\n\n{quiz.q}", quiz_keyboard(quiz.options))

    def _over_rate_limit(self, user_id: int) -> bool:
        times = self._request_times[user_id]
        return len(times) == RATE_LIMIT and time.monotonic() - times[0] < RATE_WINDOW_SECONDS

    def _allow_request(self, user_id: int) -> bool:
        if self._over_rate_limit(user_id):
            return False
        self._request_times[user_id].append(time.monotonic())
        return True

    async def _voice_text(self, member: AssistantMember, voice: Voice) -> str | None:
        """Turn a voice note into text and show it, or explain why not and return None."""
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

    @staticmethod
    def _today() -> date:
        return (datetime.now(UTC) + timedelta(hours=settings.luna_utc_offset_hours)).date()

    async def _chat(
        self, member: AssistantMember, text: str, family_note: str | None = None, speak: bool = False
    ) -> None:
        user_id = member.telegram_id
        if not self._allow_request(user_id):
            await self._send(user_id, TOO_FAST)
            return
        if member.role == "kid" and needs_adult_attention(text):
            await self._alert_parents(member, text)
            await self._send(user_id, SAFETY_REPLY)
            return
        if member.role in access.FAMILY_ROLES:
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
        async with self._locks[user_id]:
            async with (
                ChatActionSender.typing(chat_id=user_id, bot=self._bot),  # repeats while the model works
                cat_is_thinking(self._bot, user_id),
            ):
                history = await self._conversation(member)
                facts = await self._memory.recall(user_id, member.role, text) if self._memory else []
                options = ReplyOptions(
                    age=access.age_of(current, today),
                    local_only=access.money_limit_reached(current, today),
                    on_cost=costs.append if current.role == "guest" else None,
                    family_facts=await self._memory.family.recall(current.role, text) if self._memory else (),
                    spoken=speak,
                )
                reply = await self._answer(member.role, user_id, history, text, facts, options)
            self._history[user_id].append({"role": "user", "content": text})
            self._history[user_id].append({"role": "assistant", "content": reply})
            if self._memory:
                await self._memory.save_exchange(user_id, text, reply, HISTORY_MESSAGES)
        if any(costs):
            async with self._sessions() as db:
                await access.record_spend(db, user_id, sum(costs), today)
        await send_model_reply(self._bot, user_id, reply)
        if speak:
            await self._send_voice_reply(user_id, reply)
        if self._memory:
            task = asyncio.create_task(self._memory.remember(user_id, member.role, text))
            self._background.add(task)  # the event loop keeps only weak references to tasks
            task.add_done_callback(self._background.discard)

    async def _send_voice_reply(self, user_id: int, reply: str) -> None:
        """Say the reply after showing it; the text is already there if this fails."""
        spoken = await self._speak(reply)
        if spoken is None:
            return
        try:
            await self._bot.send_voice(user_id, BufferedInputFile(spoken.ogg, "luna.ogg"), duration=spoken.seconds)
        except TelegramAPIError as exc:
            logger.warning("could not send a voice reply: %s", type(exc).__name__)

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
        rows.append([InlineKeyboardButton(text="🏠 Общая память", callback_data="fam:0")])
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
        text = f"🧠 Что я о тебе помню ({len(facts)}):\n\n" + "\n".join(lines) + "\n\nНажми номер, чтобы удалить."
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
        text = (
            f"🎉 Родители добавили {status['minutes']} минут!"
            if status["status"] == "approved"
            else "Родители пока не добавили время. Поговори с ними 💬"
        )
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
