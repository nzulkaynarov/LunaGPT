"""Telegram message handling for Luna AI Assistant."""

import asyncio
import logging
import re
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lunagpt.assistant import access
from lunagpt.assistant.llm import QuizQuestion
from lunagpt.assistant.pi_client import PiClient, PiConflictError, PiNotFoundError, PiUnavailableError
from lunagpt.assistant.telegram import send_long
from lunagpt.config import settings
from lunagpt.models import AssistantMember

logger = logging.getLogger("lunagpt.assistant.bot")

AnswerFn = Callable[[str, int, list[dict[str, str]], str], Awaitable[str]]
QuizFn = Callable[[int], Awaitable[QuizQuestion | None]]

BTN_MY_TIME = "⏳ Моё время"
BTN_ASK_TIME = "🙏 Попросить время"
BTN_QUIZ = "🧠 Викторина +15 мин"
TIME_POLL_SECONDS = 30
TIME_REQUEST_TTL = timedelta(hours=24)
PI_OFFLINE = "Сейчас домашняя сеть не отвечает, попробуй чуть позже."
NO_DEVICE = "Я не нашла твоё устройство в домашней сети. Попроси родителей привязать его."

COMMANDS = [("start", "Начать"), ("help", "Что умеет Luna")]
HISTORY_MESSAGES = 8
RATE_LIMIT = 10
RATE_WINDOW_SECONDS = 60
UNKNOWN_REPLY_INTERVAL_SECONDS = 3600
INVITE_USAGE = "Формат: /invite guest Анна 6, или /invite kid Миша, или /invite adult Мама"

NOT_INVITED = "Luna работает только по приглашениям. Попроси ссылку у родителей."
BAD_INVITE = "Приглашение недействительно или уже использовано. Попроси новое."
LIMIT_REACHED = "На сегодня лимит запросов закончился. Приходи завтра 🌙"
TOO_FAST = "Слишком много сообщений подряд. Подожди минуту 🙂"
TEXT_ONLY = "Пока я понимаю только текст."
HELP_TEXT = {
    "owner": (
        "Пиши мне любые вопросы. Найду свежее в интернете.\n"
        "/invite — пригласить\n/members — участники\n/remove <id> — удалить"
    ),
    "adult": "Пиши мне любые вопросы. Найду свежее в интернете.\n/invite guest <имя> <часы> — пригласить гостя",
    "kid": "Спрашивай что угодно: уроки, игры, код, идеи. Могу поискать в интернете.\nКнопки внизу покажут твоё время.",
    "guest": "Спрашивай что угодно. Могу поискать в интернете.",
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


def needs_adult_attention(text: str) -> bool:
    return bool(SAFETY_PATTERN.search(text))


def kid_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_MY_TIME), KeyboardButton(text=BTN_ASK_TIME)],
            [KeyboardButton(text=BTN_QUIZ)],
        ],
        resize_keyboard=True,
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
    ) -> None:
        self._bot = bot
        self._sessions = session_factory
        self._answer = answer
        self._make_quiz = make_quiz
        self._pi = pi
        self._username = bot_username
        self._history: dict[int, deque[dict[str, str]]] = defaultdict(lambda: deque(maxlen=HISTORY_MESSAGES))
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._request_times: dict[int, deque[float]] = defaultdict(lambda: deque(maxlen=RATE_LIMIT))
        self._unknown_replied: dict[int, float] = {}
        self._quizzes: dict[int, QuizQuestion] = {}

    async def _send(
        self, chat_id: int, text: str, reply_markup: ReplyKeyboardMarkup | InlineKeyboardMarkup | None = None
    ) -> None:
        await send_long(self._bot, chat_id, text, reply_markup)

    async def on_message(self, message: Message) -> None:
        if message.chat.type != "private" or message.from_user is None:
            return
        user_id = message.from_user.id
        text = (message.text or "").strip()

        if text.startswith("/start") and len(text.split()) == 2:
            await self._redeem(user_id, text.split()[1])
            return

        async with self._sessions() as db:
            member = await access.get_member(db, user_id)
        if member is None:
            await self._reject_unknown(user_id)
            return
        if not text:
            await self._send(user_id, TEXT_ONLY)
            return
        if member.role == "kid" and await self._kid_action(member, text):
            return
        if text.startswith("/"):
            await self._command(member, text)
            return
        await self._chat(member, text)

    async def on_callback(self, callback: CallbackQuery) -> None:
        user_id = callback.from_user.id
        async with self._sessions() as db:
            member = await access.get_member(db, user_id)
        if member is None or not (callback.data or "").startswith("quiz:"):
            await callback.answer("Нет доступа", show_alert=True)
            return
        await callback.answer()
        quiz = self._quizzes.pop(user_id, None)
        if quiz is None:
            await self._send(user_id, "Этот вопрос уже закрыт.")
            return
        correct = int((callback.data or "quiz:-1").split(":")[1]) == quiz.correct
        await self._send(user_id, await self._quiz_outcome(member, quiz, correct))

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

    async def _kid_action(self, member: AssistantMember, text: str) -> bool:
        parts = text.split(maxsplit=1)
        if text == BTN_MY_TIME or parts[0] == "/quota":
            await self._my_time(member)
        elif text == BTN_ASK_TIME or parts[0] == "/time":
            await self._ask_time(member, parts[1] if parts[0] == "/time" and len(parts) > 1 else "")
        elif text == BTN_QUIZ or parts[0] == "/quiz":
            await self._start_quiz(member)
        else:
            return False
        return True

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
        quiz = self._quizzes.get(user_id) or await self._make_quiz(user_id)
        if quiz is None:
            await self._send(user_id, "Не получилось придумать вопрос, попробуй чуть позже.")
            return
        self._quizzes[user_id] = quiz
        await self._send(user_id, f"🧠 {quiz.topic}\n\n{quiz.q}", quiz_keyboard(quiz.options))

    async def _redeem(self, user_id: int, token: str) -> None:
        async with self._sessions() as db:
            member = await access.redeem_invite(db, token, user_id)
        if member is None:
            await self._send(user_id, BAD_INVITE)
            return
        keyboard = kid_keyboard() if member.role == "kid" else None
        await self._send(user_id, f"Привет, {member.name}! Я Luna.\n{HELP_TEXT[member.role]}", keyboard)

    async def _reject_unknown(self, user_id: int) -> None:
        now = time.monotonic()
        last = self._unknown_replied.get(user_id)
        if last is not None and now - last < UNKNOWN_REPLY_INTERVAL_SECONDS:
            return
        self._unknown_replied[user_id] = now
        await self._send(user_id, NOT_INVITED)

    async def _command(self, member: AssistantMember, text: str) -> None:
        parts = text.split()
        command = parts[0][1:].split("@")[0].lower()
        if command == "invite":
            await self._invite(member, parts[1:])
        elif command == "members" and member.role == "owner":
            await self._members(member)
        elif command == "remove" and member.role == "owner":
            await self._remove(member, parts[1:])
        else:
            keyboard = kid_keyboard() if member.role == "kid" else None
            await self._send(member.telegram_id, HELP_TEXT[member.role], keyboard)

    async def _invite(self, member: AssistantMember, args: list[str]) -> None:
        if len(args) < 2 or args[0] not in access.ROLES:
            await self._send(member.telegram_id, INVITE_USAGE)
            return
        role = args[0]
        if not access.can_invite(member.role, role):
            await self._send(member.telegram_id, "Такое приглашение может создать только владелец.")
            return
        hours = int(args[-1]) if len(args) > 2 and args[-1].isdigit() else None
        name = " ".join(args[1:-1] if hours is not None else args[1:])[:100]
        if role == "kid" and not await self._known_on_pi(name):
            await self._send(member.telegram_id, f"В HomeSec нет человека «{name}». Укажи имя точно как в панели.")
            return
        async with self._sessions() as db:
            token = await access.create_invite(db, name, role, hours)
        await self._send(
            member.telegram_id,
            f"Ссылка для {name} ({role}), действует 72 часа, один раз:\nhttps://t.me/{self._username}?start={token}",
        )

    async def _known_on_pi(self, name: str) -> bool:
        """Children must match a HomeSec person; an offline Pi does not block inviting."""
        try:
            await self._pi.quiz_available(name)
        except PiNotFoundError:
            return False
        except (PiUnavailableError, PiConflictError):
            return True
        return True

    async def _members(self, member: AssistantMember) -> None:
        async with self._sessions() as db:
            members = await access.list_members(db)
        lines = [f"{m.name} — {m.role}, id {m.telegram_id}, запросов сегодня {m.requests_today}" for m in members]
        await self._send(member.telegram_id, "\n".join(lines))

    async def _remove(self, member: AssistantMember, args: list[str]) -> None:
        if len(args) != 1 or not args[0].isdigit() or int(args[0]) == member.telegram_id:
            await self._send(member.telegram_id, "Формат: /remove <id>, себя удалить нельзя.")
            return
        async with self._sessions() as db:
            removed = await access.remove_member(db, int(args[0]))
        await self._send(member.telegram_id, "Удалено." if removed else "Такого участника нет.")

    def _allow_request(self, user_id: int) -> bool:
        now = time.monotonic()
        times = self._request_times[user_id]
        if len(times) == RATE_LIMIT and now - times[0] < RATE_WINDOW_SECONDS:
            return False
        times.append(now)
        return True

    @staticmethod
    def _today() -> date:
        return (datetime.now(UTC) + timedelta(hours=settings.luna_utc_offset_hours)).date()

    async def _chat(self, member: AssistantMember, text: str) -> None:
        user_id = member.telegram_id
        if not self._allow_request(user_id):
            await self._send(user_id, TOO_FAST)
            return
        if member.role == "kid" and needs_adult_attention(text):
            await self._alert_parents(member, text)
            await self._send(user_id, SAFETY_REPLY)
            return
        async with self._sessions() as db:
            current = await access.get_member(db, user_id)
            if current is None or not await access.consume_daily_request(db, current, self._today()):
                await self._send(user_id, LIMIT_REACHED)
                return
        async with self._locks[user_id]:
            await self._bot.send_chat_action(user_id, "typing")
            history = list(self._history[user_id])
            reply = await self._answer(member.role, user_id, history, text)
            self._history[user_id].append({"role": "user", "content": text})
            self._history[user_id].append({"role": "assistant", "content": reply})
        await self._send(user_id, reply)

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
