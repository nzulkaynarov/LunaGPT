"""⏳ Интернет for children: what keeps them offline now, asking parents for time and the quiz.

Parents decide on the Pi (HomeSec); Luna asks it by the person's id and reports the decisions back.
A quiz question waits in memory until answered; a restart loses it and the child asks for a new one."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lunagpt.assistant import access
from lunagpt.assistant.llm import QuizQuestion
from lunagpt.assistant.pi_client import PiClient, PiConflictError, PiNotFoundError, PiUnavailableError
from lunagpt.assistant.telegram import send_long
from lunagpt.models import AssistantMember

logger = logging.getLogger("lunagpt.assistant.internet")

QuizFn = Callable[[int, int | None], Awaitable[QuizQuestion | None]]

INTERNET_PREFIXES = {"nta", "ntq", "quiz"}
BTN_ASK_TIME = "🙏 Попросить время"
BTN_QUIZ = "🎯 Викторина +15 мин"
TIME_POLL_SECONDS = 30
TIME_REQUEST_TTL = timedelta(hours=24)
PI_OFFLINE = "Сейчас домашняя сеть не отвечает, попробуй чуть позже."
NO_DEVICE = "Я не нашла твоё устройство в домашней сети. Попроси родителей привязать его."


def pi_person(member: AssistantMember) -> str:
    """How the Pi knows this person: by id when linked, which survives renames, else by name."""
    return str(member.homesec_person_id) if member.homesec_person_id is not None else member.name


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


def _inline(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def internet_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_inline(BTN_ASK_TIME, "nta")], [_inline(BTN_QUIZ, "ntq")]])


def quiz_keyboard(options: list[str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=o[:60], callback_data=f"quiz:{i}")] for i, o in enumerate(options)]
    )


class InternetDesk:
    """The child's ⏳ Интернет screen and its buttons."""

    def __init__(
        self,
        bot: Bot,
        session_factory: async_sessionmaker[AsyncSession],
        pi: PiClient,
        make_quiz: QuizFn,
        today: Callable[[], date],
    ) -> None:
        self._bot = bot
        self._sessions = session_factory
        self._pi = pi
        self._make_quiz = make_quiz
        self._today = today
        self._quizzes: dict[int, QuizQuestion] = {}

    async def _say(self, chat_id: int, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
        await send_long(self._bot, chat_id, text, reply_markup)

    async def handle(self, callback: CallbackQuery, member: AssistantMember, prefix: str, args: list[str]) -> None:
        if prefix == "nta":
            await self.ask(member, "")
        elif prefix == "ntq":
            await self.start_quiz(member)
        elif prefix == "quiz":
            await self.answer_quiz(member, int(args[0]))

    async def answer_quiz(self, member: AssistantMember, choice: int) -> None:
        quiz = self._quizzes.pop(member.telegram_id, None)
        if quiz is None:
            await self._say(member.telegram_id, "Этот вопрос уже закрыт.")
            return
        await self._say(member.telegram_id, await self._quiz_outcome(member, quiz, choice == quiz.correct))

    async def _quiz_outcome(self, member: AssistantMember, quiz: QuizQuestion, correct: bool) -> str:
        try:
            minutes = await self._pi.quiz_result(pi_person(member), correct, quiz.topic)
        except PiConflictError:
            return "Сегодня викторина уже пройдена. Приходи завтра!"
        except (PiNotFoundError, PiUnavailableError):
            return PI_OFFLINE
        if correct:
            return f"🎉 Верно! Тебе начислено +{minutes} минут."
        return f"Не совсем. Правильный ответ: {quiz.options[quiz.correct]}. Завтра будет новый шанс!"

    async def view(self, member: AssistantMember) -> None:
        try:
            restriction = await self._pi.restriction(pi_person(member))
            rows = await self._pi.quota(pi_person(member))
        except PiNotFoundError:
            await self._say(member.telegram_id, NO_DEVICE)
            return
        except PiUnavailableError:
            await self._say(member.telegram_id, PI_OFFLINE)
            return
        lines = [describe_restriction(restriction)] if restriction else []
        lines += [
            f"{r['device']}, {r['category_label']}: {r['used_minutes']} из {r['limit_minutes']} мин" for r in rows
        ]
        if not rows:
            lines.append("Лимитов на сегодня нет.")
        await self._say(member.telegram_id, "⏳ Интернет\n\n" + "\n".join(lines), internet_keyboard())

    async def ask(self, member: AssistantMember, reason: str) -> None:
        user_id = member.telegram_id
        try:
            restriction = await self._pi.restriction(pi_person(member))
        except PiUnavailableError:
            await self._say(user_id, PI_OFFLINE)
            return
        if restriction and restriction["kind"] == "bedtime":
            # Parents do not open bedtime at night, so nobody is woken up for it.
            await self._say(user_id, describe_restriction(restriction) + BEDTIME_GOODNIGHT)
            return
        try:
            request_id = await self._pi.request_time(pi_person(member), reason)
        except PiConflictError:
            await self._say(user_id, "Запрос уже отправлен, дождись ответа родителей.")
            return
        except PiNotFoundError:
            await self._say(user_id, NO_DEVICE)
            return
        except PiUnavailableError:
            await self._say(user_id, PI_OFFLINE)
            return
        async with self._sessions() as db:
            await access.track_time_request(db, request_id, user_id)
        if restriction:
            text = "✅ Попросила родителей открыть интернет. Сообщу, когда они ответят."
            await self._say(user_id, f"{describe_restriction(restriction)}\n\n{text}")
            return  # quiz minutes would not lift a pause
        text = "✅ Запрос отправлен родителям. Сообщу, когда они ответят."
        quiz = None
        try:
            if await self._pi.quiz_available(pi_person(member)):
                text += f"\n\nПока ждёшь, можно заработать +15 минут: нажми «{BTN_QUIZ}»."
                quiz = InlineKeyboardMarkup(inline_keyboard=[[_inline(BTN_QUIZ, "ntq")]])
        except (PiNotFoundError, PiUnavailableError):
            pass
        await self._say(user_id, text, quiz)

    async def start_quiz(self, member: AssistantMember) -> None:
        user_id = member.telegram_id
        try:
            available = await self._pi.quiz_available(pi_person(member))
        except PiNotFoundError:
            await self._say(user_id, NO_DEVICE)
            return
        except PiUnavailableError:
            await self._say(user_id, PI_OFFLINE)
            return
        if not available:
            await self._say(user_id, "Сегодня викторина уже пройдена. Приходи завтра!")
            return
        quiz = self._quizzes.get(user_id) or await self._make_quiz(user_id, access.age_of(member, self._today()))
        if quiz is None:
            await self._say(user_id, "Не получилось придумать вопрос, попробуй чуть позже.")
            return
        self._quizzes[user_id] = quiz
        await self._say(user_id, f"🎯 {quiz.topic}\n\n{quiz.q}", quiz_keyboard(quiz.options))


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
        state = status.get("status") if isinstance(status, dict) else None
        if state == "pending":
            if now - request.created_at > TIME_REQUEST_TTL:
                async with session_factory() as db:
                    await access.finish_time_request(db, request.request_id)
            continue
        if state != "approved":
            text = "Родители пока не добавили время. Поговори с ними 💬"
        elif status.get("category") == "pause":
            text = f"🎉 Родители открыли интернет на {status.get('minutes', 0)} минут!"
        else:
            text = f"🎉 Родители добавили {status.get('minutes', 0)} минут!"
        try:
            await send_long(bot, request.telegram_id, text)
        except TelegramAPIError as exc:  # a blocked bot must not hold up everyone else's decisions
            logger.warning("could not report a time decision: %s", type(exc).__name__)
        async with session_factory() as db:
            await access.finish_time_request(db, request.request_id)


async def watch_time_requests(bot: Bot, pi: PiClient, session_factory: async_sessionmaker[AsyncSession]) -> None:
    while True:
        try:
            await report_time_decisions(bot, pi, session_factory)
        except (TelegramAPIError, OSError) as exc:
            logger.warning("time decision report failed: %s", type(exc).__name__)
        await asyncio.sleep(TIME_POLL_SECONDS)
