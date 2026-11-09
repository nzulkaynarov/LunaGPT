"""Telegram message handling for Luna AI Assistant."""

import asyncio
import logging
import re
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    ForceReply,
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
from lunagpt.assistant.vendors import vendor_of
from lunagpt.config import settings
from lunagpt.models import AssistantMember

logger = logging.getLogger("lunagpt.assistant.bot")

AnswerFn = Callable[[str, int, list[dict[str, str]], str], Awaitable[str]]
QuizFn = Callable[[int], Awaitable[QuizQuestion | None]]

BTN_MY_TIME = "⏳ Моё время"
BTN_ASK_TIME = "🙏 Попросить время"
BTN_QUIZ = "🧠 Викторина +15 мин"
BTN_FAMILY = "👨‍👩‍👧 Семья"
BTN_SHARED = "📺 Общие устройства"
BTN_DEVICES = "🗂 Устройства"
BTN_GUEST = "➕ Гость"
BTN_MEMBERS = "👥 Участники"
BTN_HELP = "❓ Помощь"
MENU_BUTTONS = {
    BTN_MY_TIME,
    BTN_ASK_TIME,
    BTN_QUIZ,
    BTN_FAMILY,
    BTN_SHARED,
    BTN_DEVICES,
    BTN_GUEST,
    BTN_MEMBERS,
    BTN_HELP,
}
GUEST_HOURS = (3, 6, 24, 72)
SHARED_LABELS = {"kid": "👶 Детский режим", "off": "⛔ Выключено", "adult": "🍿 Для взрослых"}
TRIAGE_KIND_LABELS = {"shared": "общее устройство", "iot": "умный дом"}
TIME_POLL_SECONDS = 30
TIME_REQUEST_TTL = timedelta(hours=24)
PI_OFFLINE = "Сейчас домашняя сеть не отвечает, попробуй чуть позже."
NO_DEVICE = "Я не нашла твоё устройство в домашней сети. Попроси родителей привязать его."

HISTORY_MESSAGES = 8
RATE_LIMIT = 10
RATE_WINDOW_SECONDS = 60
UNKNOWN_REPLY_INTERVAL_SECONDS = 3600
NOT_INVITED = "Luna работает только по приглашениям. Попроси ссылку у родителей."
BAD_INVITE = "Приглашение недействительно или уже использовано. Попроси новое."
LIMIT_REACHED = "На сегодня лимит запросов закончился. Приходи завтра 🌙"
TOO_FAST = "Слишком много сообщений подряд. Подожди минуту 🙂"
TEXT_ONLY = "Пока я понимаю только текст."
HELP_TEXT = {
    "owner": "Пиши мне любые вопросы, найду свежее в интернете. Семья, устройства и гости — на кнопках внизу.",
    "adult": "Пиши мне любые вопросы, найду свежее в интернете. Устройства и гости — на кнопках внизу.",
    "kid": "Спрашивай что угодно: уроки, игры, код, идеи. Могу поискать в интернете. Твоё время — на кнопках внизу.",
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


class GuestInvite(StatesGroup):
    name = State()


def main_keyboard(role: str) -> ReplyKeyboardMarkup:
    """The persistent menu; server-side checks still decide what each role may do."""
    layouts = {
        "owner": [[BTN_FAMILY, BTN_SHARED], [BTN_DEVICES, BTN_GUEST], [BTN_MEMBERS, BTN_HELP]],
        "adult": [[BTN_SHARED, BTN_GUEST], [BTN_HELP]],
        "kid": [[BTN_MY_TIME, BTN_ASK_TIME], [BTN_QUIZ, BTN_HELP]],
        "guest": [[BTN_HELP]],
    }
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=text) for text in row] for row in layouts[role]],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Напиши Luna…",
    )


def triage_keyboard(device_id: int, index: int, total: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="👤 Чьё это?", callback_data=f"dvp:{device_id}:{index}")],
        [
            InlineKeyboardButton(text="📺 Общее", callback_data=f"dvc:{device_id}:shared:{index}"),
            InlineKeyboardButton(text="🔌 Умный дом", callback_data=f"dvc:{device_id}:iot:{index}"),
        ],
    ]
    if total > 1:
        rows.append([InlineKeyboardButton(text="⏭ Дальше", callback_data=f"dv:{index + 1}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def triage_card_text(group: dict[str, Any], index: int, total: int, vendor: str | None) -> str:
    lines = [f"🗂 Разбор устройств: {index + 1} из {total}", "", f"«{group['name']}»"]
    if group["count"] > 1:
        lines.append(f"Записей: {group['count']}. Адрес приватный и меняется, поэтому устройство появлялось много раз.")
    elif group["mac_random"]:
        lines.append("Адрес приватный, производителя не определить.")
    if vendor:
        lines.append(f"Производитель: {vendor}")
    if group["ip"]:
        lines.append(f"Последний адрес в сети: {group['ip']}")
    lines.append(f"Последняя новая запись: {group['last_seen'].replace('T', ' ')}")
    if group["owners"]:
        lines.append("С таким именем уже есть у: " + ", ".join(owner["name"] for owner in group["owners"]))
    return "\n".join(lines)


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


def guest_hours_keyboard() -> InlineKeyboardMarkup:
    labels = {3: "3 часа", 6: "6 часов", 24: "Сутки", 72: "3 дня"}
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=labels[h], callback_data=f"guest:{h}") for h in GUEST_HOURS]]
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
        self,
        chat_id: int,
        text: str,
        reply_markup: ReplyKeyboardMarkup | InlineKeyboardMarkup | ForceReply | None = None,
    ) -> None:
        await send_long(self._bot, chat_id, text, reply_markup)

    async def on_message(self, message: Message, state: FSMContext | None = None) -> None:
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
        if state is not None and await state.get_state() == GuestInvite.name.state:
            await state.clear()
            if text not in MENU_BUTTONS and not text.startswith("/"):
                await self._ask_guest_hours(member, text[:100], state)
                return
        if await self._menu_action(member, text, state):
            return
        if text.startswith("/"):
            await self._send(user_id, HELP_TEXT[member.role], main_keyboard(member.role))
            return
        await self._chat(member, text)

    async def _menu_action(self, member: AssistantMember, text: str, state: FSMContext | None) -> bool:
        """Run the pressed menu button if this role is allowed to use it."""
        adult = member.role in access.ADULT_ROLES
        if text == BTN_HELP:
            await self._send(member.telegram_id, HELP_TEXT[member.role], main_keyboard(member.role))
        elif text == BTN_FAMILY and member.role == "owner":
            await self._family(member)
        elif text == BTN_MEMBERS and member.role == "owner":
            await self._send_members(member)
        elif text == BTN_SHARED and adult:
            await self._send_shared_list(member)
        elif text == BTN_DEVICES and member.role == "owner":
            await self._send(member.telegram_id, *await self._triage_view(0))
        elif text == BTN_GUEST and adult and state is not None:
            await state.set_state(GuestInvite.name)
            await self._send(
                member.telegram_id,
                "Как зовут гостя?",
                ForceReply(input_field_placeholder="Имя гостя"),
            )
        elif member.role == "kid" and text == BTN_MY_TIME:
            await self._my_time(member)
        elif member.role == "kid" and text == BTN_ASK_TIME:
            await self._ask_time(member, "")
        elif member.role == "kid" and text == BTN_QUIZ:
            await self._start_quiz(member)
        else:
            return False
        return True

    async def on_callback(self, callback: CallbackQuery, state: FSMContext | None = None) -> None:
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
        elif prefix == "link":
            await self._send_family_link(member, int(args[0]))
        elif prefix == "guest":
            await self._finish_guest_invite(member, callback, int(args[0]), state)
        elif prefix == "shl":
            await self._edit_shared_list(callback)
        elif prefix == "sh":
            await self._show_shared_device(callback, int(args[0]))
        elif prefix == "shs":
            await self._change_shared_device(callback, int(args[0]), args[1])
        elif prefix == "dv":
            await self._edit(callback, *await self._triage_view(int(args[0])))
        elif prefix == "dvp":
            await self._edit(callback, *await self._triage_people_view(int(args[0]), int(args[1])))
        elif prefix == "dvc":
            await self._choose_destination(callback, int(args[0]), args[1], int(args[2]))
        elif prefix == "dvy":
            await self._apply_destination(callback, int(args[0]), args[1], int(args[2]))
        elif prefix == "rml":
            await self._edit(callback, *await self._members_view(member))
        elif prefix == "rm":
            await self._confirm_remove(callback, int(args[0]))
        elif prefix == "rmy":
            await self._remove_member(member, callback, int(args[0]))

    @staticmethod
    def _callback_allowed(role: str, prefix: str) -> bool:
        if prefix == "quiz":
            return role == "kid"
        if prefix in ("link", "rm", "rmy", "rml", "dv", "dvp", "dvc", "dvy"):
            return role == "owner"
        if prefix in ("guest", "sh", "shs", "shl"):
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

    async def _ask_guest_hours(self, member: AssistantMember, name: str, state: FSMContext) -> None:
        await state.update_data(guest_name=name)
        await self._send(member.telegram_id, f"На сколько пустить: {name}?", guest_hours_keyboard())

    async def _finish_guest_invite(
        self, member: AssistantMember, callback: CallbackQuery, hours: int, state: FSMContext | None
    ) -> None:
        name = str((await state.get_data()).get("guest_name", "")) if state is not None else ""
        if not name:
            await self._edit(callback, "Приглашение устарело, начни заново.", None)
            return
        async with self._sessions() as db:
            token = await access.create_invite(db, name, "guest", hours)
        if state is not None:
            await state.clear()
        link = f"https://t.me/{self._username}?start={token}"
        await self._edit(callback, f"Перешли ссылку гостю ({name}). Одноразовая, доступ на {hours} ч:\n{link}", None)

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

    async def _triage_view(self, index: int, notice: str = "") -> tuple[str, InlineKeyboardMarkup | None]:
        prefix = f"{notice}\n\n" if notice else ""
        try:
            groups = await self._pi.device_triage()
        except (PiNotFoundError, PiUnavailableError):
            return PI_OFFLINE, None
        if not groups:
            return f"{prefix}Все устройства разобраны ✅", None
        index %= len(groups)
        group = groups[index]
        vendor = None if group["mac_random"] else await vendor_of(group["mac"])
        text = prefix + triage_card_text(group, index, len(groups), vendor)
        return text, triage_keyboard(int(group["id"]), index, len(groups))

    async def _triage_group(self, device_id: int) -> dict[str, Any] | None:
        return next((g for g in await self._pi.device_triage() if int(g["id"]) == device_id), None)

    async def _triage_people_view(self, device_id: int, index: int) -> tuple[str, InlineKeyboardMarkup | None]:
        try:
            group = await self._triage_group(device_id)
            people = await self._pi.people()
        except (PiNotFoundError, PiUnavailableError):
            return PI_OFFLINE, None
        if group is None:
            return await self._triage_view(index, "Это устройство уже разобрали.")
        owners = {int(owner["id"]) for owner in group["owners"]}
        people.sort(key=lambda person: (int(person["id"]) not in owners, str(person["name"])))
        buttons = [
            InlineKeyboardButton(text=str(p["name"])[:30], callback_data=f"dvc:{device_id}:{p['id']}:{index}")
            for p in people
        ]
        rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
        rows.append([InlineKeyboardButton(text="← Назад", callback_data=f"dv:{index}")])
        return f"Чьё устройство «{group['name']}»?", InlineKeyboardMarkup(inline_keyboard=rows)

    async def _destination_label(self, destination: str) -> str:
        if destination in TRIAGE_KIND_LABELS:
            return TRIAGE_KIND_LABELS[destination]
        people = await self._pi.people()
        return next((str(p["name"]) for p in people if str(p["id"]) == destination), "выбранному человеку")

    async def _choose_destination(self, callback: CallbackQuery, device_id: int, destination: str, index: int) -> None:
        """Folding many records cannot be undone with a button, so it asks first."""
        try:
            group = await self._triage_group(device_id)
            if group is None:
                await self._edit(callback, *await self._triage_view(index, "Это устройство уже разобрали."))
                return
            if group["count"] == 1:
                await self._apply_destination(callback, device_id, destination, index)
                return
            label = await self._destination_label(destination)
        except (PiNotFoundError, PiUnavailableError):
            await self._edit(callback, PI_OFFLINE, None)
            return
        text = (
            f"Объединить {group['count']} записей «{group['name']}» в одно устройство и записать: {label}?\n\n"
            "Если такие устройства есть у нескольких людей, все записи достанутся одному. "
            "Сеть сама заведёт новую запись, когда устройство сменит адрес."
        )
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Да", callback_data=f"dvy:{device_id}:{destination}:{index}")],
                [InlineKeyboardButton(text="← Назад", callback_data=f"dv:{index}")],
            ]
        )
        await self._edit(callback, text, markup)

    async def _apply_destination(self, callback: CallbackQuery, device_id: int, destination: str, index: int) -> None:
        try:
            result = await self._pi.sort_devices(device_id, destination)
        except PiNotFoundError:
            await self._edit(callback, *await self._triage_view(index, "Это устройство уже разобрали."))
            return
        except PiConflictError:
            await self._edit(callback, *await self._triage_view(index + 1, "Это устройство сети, его не трогаю."))
            return
        except PiUnavailableError:
            await self._edit(callback, PI_OFFLINE, None)
            return
        where = result["person"] or TRIAGE_KIND_LABELS[result["kind"]]
        await self._edit(callback, *await self._triage_view(index, f"✅ «{result['name']}» → {where}"))

    async def _members_view(self, member: AssistantMember) -> tuple[str, InlineKeyboardMarkup | None]:
        async with self._sessions() as db:
            members = await access.list_members(db)
        rows = [
            [InlineKeyboardButton(text=f"❌ {m.name} · {m.role}"[:60], callback_data=f"rm:{m.telegram_id}")]
            for m in members
            if m.telegram_id != member.telegram_id
        ]
        text = "Участники Luna. Нажми, чтобы отключить:" if rows else "Кроме тебя участников пока нет."
        return text, InlineKeyboardMarkup(inline_keyboard=rows) if rows else None

    async def _send_members(self, member: AssistantMember) -> None:
        text, markup = await self._members_view(member)
        await self._send(member.telegram_id, text, markup)

    async def _confirm_remove(self, callback: CallbackQuery, telegram_id: int) -> None:
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Да, отключить", callback_data=f"rmy:{telegram_id}", style="danger"),
                    InlineKeyboardButton(text="Отмена", callback_data="rml"),
                ]
            ]
        )
        await self._edit(callback, "Отключить этого участника от Luna?", markup)

    async def _remove_member(self, member: AssistantMember, callback: CallbackQuery, telegram_id: int) -> None:
        if telegram_id != member.telegram_id:
            async with self._sessions() as db:
                await access.remove_member(db, telegram_id)
        await self._edit(callback, *await self._members_view(member))

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
        if member.homesec_person_id is not None:
            await self._link_on_pi(member)
        await self._send(
            user_id, f"Привет, {member.name}! Я Luna.\n{HELP_TEXT[member.role]}", main_keyboard(member.role)
        )

    async def _link_on_pi(self, member: AssistantMember) -> None:
        """Store the Telegram account on the Pi; the periodic sync retries if it is offline."""
        try:
            await self._pi.link_telegram(int(member.homesec_person_id or 0), member.telegram_id)
        except PiConflictError:
            await self._send(member.telegram_id, "Этот Telegram уже привязан к другому человеку в HomeSec.")
        except (PiNotFoundError, PiUnavailableError) as exc:
            logger.warning("could not link telegram on the pi: %s", type(exc).__name__)

    async def _family(self, member: AssistantMember) -> None:
        try:
            people = await self._pi.people()
        except (PiNotFoundError, PiUnavailableError):
            await self._send(member.telegram_id, PI_OFFLINE)
            return
        rows = [
            [
                InlineKeyboardButton(
                    text=f"{'✅' if p['telegram_id'] else '➕'} {p['name']} · {p['role']} · устройств {p['devices']}"[
                        :60
                    ],
                    callback_data=f"link:{p['id']}",
                )
            ]
            for p in people
        ]
        await self._send(
            member.telegram_id,
            "Семья из HomeSec. Выбери человека, чтобы получить его личную ссылку:",
            InlineKeyboardMarkup(inline_keyboard=rows),
        )

    async def _send_family_link(self, member: AssistantMember, person_id: int) -> None:
        try:
            person = next((p for p in await self._pi.people() if int(p["id"]) == person_id), None)
        except (PiNotFoundError, PiUnavailableError):
            await self._send(member.telegram_id, PI_OFFLINE)
            return
        if person is None:
            await self._send(member.telegram_id, "Такого человека больше нет в HomeSec.")
            return
        role = access.PERSON_ROLE_TO_MEMBER_ROLE.get(person["role"], "guest")
        async with self._sessions() as db:
            token = await access.create_invite(db, person["name"], role, person_id=person_id)
        await self._send(
            member.telegram_id,
            f"Перешли эту ссылку: {person['name']}. Одноразовая, действует 72 часа:\n"
            f"https://t.me/{self._username}?start={token}",
        )

    async def _reject_unknown(self, user_id: int) -> None:
        now = time.monotonic()
        last = self._unknown_replied.get(user_id)
        if last is not None and now - last < UNKNOWN_REPLY_INTERVAL_SECONDS:
            return
        self._unknown_replied[user_id] = now
        await self._send(user_id, NOT_INVITED)

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
