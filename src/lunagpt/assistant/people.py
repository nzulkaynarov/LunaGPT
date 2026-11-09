"""The single way in: applications, and the adults' overview of who is who."""

import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lunagpt.assistant import access
from lunagpt.assistant.pi_client import PiClient, PiConflictError, PiNotFoundError, PiUnavailableError
from lunagpt.models import AssistantApplication, AssistantMember

logger = logging.getLogger("lunagpt.assistant.people")

WelcomeFn = Callable[[int, str, str], Awaitable[None]]
View = tuple[str, InlineKeyboardMarkup | None]

PEOPLE_PREFIXES = frozenset({"app", "apf", "apg", "apr", "pl", "pls", "pc", "pgf", "pgp", "px", "pxy", "prb"})
REPLY_INTERVAL_SECONDS = 3600
NOTIFICATIONS_PER_HOUR = 10
MAX_LISTED = 30
BTN_SHARE_CONTACT = "📱 Отправить мой контакт"
ASK_CONTACT = (
    "Привет! Я Luna, семейный ИИ-ассистент. Чтобы получить доступ, отправь свой контакт кнопкой ниже. "
    "Заявку рассмотрит взрослый из семьи."
)
NOT_YOUR_CONTACT = "Нужен твой собственный контакт. Нажми кнопку ниже."
APPLIED = "Заявка отправлена. Напишу, когда взрослый примет решение."
STILL_WAITING = "Заявка ещё ждёт решения взрослых."
FULL = "Сейчас заявки не принимаются. Попробуй позже."
CLOSED = "Доступ к Luna закрыт."
TURNED_DOWN = "К сожалению, доступ к Luna не открыт."
ALREADY_DECIDED = "По этой заявке уже приняли решение."
NETWORK_DOWN = "Домашняя сеть не отвечает, семью выбрать пока нельзя."
ROLE_LABELS = {"owner": "глава семьи", "adult": "взрослый", "kid": "ребёнок", "guest": "гость"}
SECTION_TITLES = {"f": "Семья", "g": "Гости", "p": "В ожидании", "r": "Отказано"}
INVITE_TEXT = "Присоединяйся к Luna: нажми «Старт» и отправь свой контакт."


def contact_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_SHARE_CONTACT, request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def application_text(application: AssistantApplication) -> str:
    lines = [f"🔔 Заявка в Luna: {application.name}"]
    if application.username:
        lines.append(f"Telegram: @{application.username}")
    if application.phone:
        lines.append(f"Телефон: {application.phone}")
    return "\n".join(lines)


def _button(text: str, data: str, style: str | None = None) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text[:60], callback_data=data, style=style)


def _pairs(buttons: list[InlineKeyboardButton]) -> list[list[InlineKeyboardButton]]:
    return [buttons[i : i + 2] for i in range(0, len(buttons), 2)]


class PeopleDesk:
    """Adults decide who is family and who is a guest; everyone else waits or stays out."""

    def __init__(
        self,
        bot: Bot,
        session_factory: async_sessionmaker[AsyncSession],
        pi: PiClient,
        bot_username: str,
        welcome: WelcomeFn,
    ) -> None:
        self._bot = bot
        self._sessions = session_factory
        self._pi = pi
        self._username = bot_username
        self._welcome = welcome
        self._replied: dict[int, float] = {}
        self._notified: deque[float] = deque(maxlen=NOTIFICATIONS_PER_HOUR)
        self._cards: dict[int, list[tuple[int, int]]] = {}

    # ---- the way in ----

    async def on_stranger(self, message: Message) -> None:
        if message.from_user is None:
            return
        user_id = message.from_user.id
        async with self._sessions() as db:
            application = await access.get_application(db, user_id)
        if application is not None:
            await self._reply_rarely(user_id, CLOSED if application.status == access.REJECTED else STILL_WAITING)
            return
        contact = message.contact
        if contact is None:
            if (message.text or "").startswith("/start") or self._may_reply(user_id):
                await self._bot.send_message(user_id, ASK_CONTACT, reply_markup=contact_keyboard())
            return
        if contact.user_id != user_id:
            await self._bot.send_message(user_id, NOT_YOUR_CONTACT, reply_markup=contact_keyboard())
            return
        name = message.from_user.full_name or contact.first_name
        username = message.from_user.username
        async with self._sessions() as db:
            created = await access.submit_application(db, user_id, name, username, contact.phone_number)
        await self._bot.send_message(user_id, APPLIED if created else FULL, reply_markup=ReplyKeyboardRemove())
        if created:
            await self._notify_adults(user_id)

    def _may_reply(self, user_id: int) -> bool:
        now = time.monotonic()
        last = self._replied.get(user_id)
        if last is not None and now - last < REPLY_INTERVAL_SECONDS:
            return False
        self._replied[user_id] = now
        return True

    async def _reply_rarely(self, user_id: int, text: str) -> None:
        if self._may_reply(user_id):
            await self._bot.send_message(user_id, text)

    def _may_notify(self) -> bool:
        """Beyond the hourly limit an application still waits in the adults' list."""
        now = time.monotonic()
        if len(self._notified) == NOTIFICATIONS_PER_HOUR and now - self._notified[0] < REPLY_INTERVAL_SECONDS:
            return False
        self._notified.append(now)
        return True

    async def _notify_adults(self, applicant_id: int) -> None:
        if not self._may_notify():
            logger.warning("application notifications are throttled")
            return
        async with self._sessions() as db:
            application = await access.get_application(db, applicant_id)
            adults = await access.adult_chat_ids(db)
        if application is None:
            return
        text, markup = await self._application_view(application)
        cards = []
        for adult_id in adults:
            try:
                sent = await self._bot.send_message(adult_id, text, reply_markup=markup)
                cards.append((adult_id, sent.message_id))
            except TelegramAPIError as exc:
                logger.warning("could not notify an adult: %s", type(exc).__name__)
        self._cards[applicant_id] = cards

    async def _free_people(self, applicant_id: int) -> list[dict[str, Any]]:
        """People on the Pi who have no Telegram account yet; guests on the Pi are not family."""
        people = await self._pi.people()
        async with self._sessions() as db:
            linked = {m.homesec_person_id for m in await access.list_members(db)}
        return [
            person
            for person in people
            if person["role"] in ("adult", "kid")
            and person["telegram_id"] in (None, applicant_id)
            and int(person["id"]) not in linked
        ]

    async def _application_view(self, application: AssistantApplication) -> View:
        user_id = application.telegram_id
        text = application_text(application)
        try:
            people = await self._free_people(user_id)
        except (PiNotFoundError, PiUnavailableError):
            rows = [[_button("🔄 Обновить", f"app:{user_id}")]]
            text += f"\n\n{NETWORK_DOWN}"
        else:
            family = [_button(f"👨‍👩‍👧 {p['name']}", f"apf:{user_id}:{p['id']}") for p in people]
            rows = _pairs(family)
            text += "\n\nКто это?"
        rows.append([_button("🏡 Гость", f"apg:{user_id}"), _button("⛔ Отклонить", f"apr:{user_id}", "danger")])
        return text, InlineKeyboardMarkup(inline_keyboard=rows)

    # ---- decisions ----

    async def _close_other_cards(self, applicant_id: int, adult_id: int, outcome: str) -> None:
        for chat_id, message_id in self._cards.pop(applicant_id, []):
            if chat_id == adult_id:
                continue
            try:
                await self._bot.edit_message_text(outcome, chat_id=chat_id, message_id=message_id)
            except TelegramAPIError as exc:
                logger.warning("could not update an application card: %s", type(exc).__name__)

    async def _finish(self, callback: CallbackQuery, actor: AssistantMember, applicant_id: int, outcome: str) -> None:
        text = f"{outcome}. Решил(а): {actor.name}"
        await self._close_other_cards(applicant_id, actor.telegram_id, text)
        await self._edit(callback, text, None)

    async def _link_person(self, telegram_id: int, person_id: int) -> tuple[dict[str, Any] | None, str]:
        """Check the choice against the Pi and write the link there; the reason explains a refusal."""
        try:
            free = await self._free_people(telegram_id)
            person = next((p for p in free if int(p["id"]) == person_id), None)
            if person is None:
                return None, "Этот человек уже занят или его нет в HomeSec."
            await self._pi.link_telegram(person_id, telegram_id)
        except PiConflictError:
            return None, "Этот Telegram уже привязан к другому человеку в HomeSec."
        except (PiNotFoundError, PiUnavailableError):
            return None, NETWORK_DOWN
        return person, ""

    async def _admit_family(
        self, callback: CallbackQuery, actor: AssistantMember, applicant_id: int, person_id: int
    ) -> None:
        async with self._sessions() as db:
            application = await access.lock_pending(db, applicant_id)
            if application is None:
                await self._edit(callback, ALREADY_DECIDED, None)
                return
            person, problem = await self._link_person(applicant_id, person_id)
            if person is None:
                view_text, markup = await self._application_view(application)
                await self._edit(callback, f"{problem}\n\n{view_text}", markup)
                return
            role = access.PERSON_ROLE_TO_MEMBER_ROLE[person["role"]]
            name = str(person["name"])
            await access.admit(db, application, name, role, person_id)
        await self._finish(callback, actor, applicant_id, f"✅ {name}: {ROLE_LABELS[role]}")
        await self._welcome(applicant_id, name, role)

    async def _admit_guest(self, callback: CallbackQuery, actor: AssistantMember, applicant_id: int) -> None:
        async with self._sessions() as db:
            application = await access.lock_pending(db, applicant_id)
            if application is None:
                await self._edit(callback, ALREADY_DECIDED, None)
                return
            name = application.name
            await access.admit(db, application, name, "guest", None)
        await self._finish(callback, actor, applicant_id, f"✅ {name}: гость")
        await self._welcome(applicant_id, name, "guest")

    async def _reject(self, callback: CallbackQuery, actor: AssistantMember, applicant_id: int) -> None:
        async with self._sessions() as db:
            application = await access.lock_pending(db, applicant_id)
            if application is None:
                await self._edit(callback, ALREADY_DECIDED, None)
                return
            name = application.name
            await access.reject(db, application, actor.telegram_id)
        await self._finish(callback, actor, applicant_id, f"⛔ {name}: отказано")
        await self._tell(applicant_id, TURNED_DOWN)

    async def _guest_to_family(
        self, callback: CallbackQuery, actor: AssistantMember, telegram_id: int, person_id: int
    ) -> None:
        async with self._sessions() as db:
            member = await db.get(AssistantMember, telegram_id, with_for_update=True)
            if member is None or member.role != "guest":
                await self._edit(callback, *await self._summary())
                return
            person, problem = await self._link_person(telegram_id, person_id)
            if person is None:
                await self._edit(callback, problem, self._back("pls:g"))
                return
            member.name = str(person["name"])
            member.role = access.PERSON_ROLE_TO_MEMBER_ROLE[person["role"]]
            member.homesec_person_id = person_id
            name, role = member.name, member.role
            await db.commit()
        await self._edit(callback, f"✅ {name}: {ROLE_LABELS[role]}", self._back("pls:f"))
        await self._welcome(telegram_id, name, role)

    @staticmethod
    def _may_exclude(actor: AssistantMember, target: AssistantMember) -> bool:
        """Adults manage guests; only the head of the family can remove family, and never themselves."""
        if target.telegram_id == actor.telegram_id or target.role == "owner":
            return False
        return bool(target.role == "guest" or actor.role == "owner")

    async def _exclude(self, callback: CallbackQuery, actor: AssistantMember, telegram_id: int) -> None:
        async with self._sessions() as db:
            member = await db.get(AssistantMember, telegram_id, with_for_update=True)
            if member is None or not self._may_exclude(actor, member):
                await self._edit(callback, "Это действие недоступно.", self._back("pl"))
                return
            name = member.name
            await access.exclude(db, member, actor.telegram_id)
        await self._edit(callback, f"⛔ {name}: доступ снят", self._back("pl"))
        await self._tell(telegram_id, TURNED_DOWN, ReplyKeyboardRemove())

    async def _reopen(self, callback: CallbackQuery, telegram_id: int) -> None:
        async with self._sessions() as db:
            application = await access.reopen(db, telegram_id)
        if application is None:
            await self._edit(callback, ALREADY_DECIDED, self._back("pl"))
            return
        await self._edit(callback, *await self._application_view(application))

    async def _tell(self, chat_id: int, text: str, markup: ReplyKeyboardRemove | None = None) -> None:
        try:
            await self._bot.send_message(chat_id, text, reply_markup=markup)
        except TelegramAPIError as exc:
            logger.warning("could not message a person: %s", type(exc).__name__)

    # ---- overview ----

    async def _summary(self) -> View:
        async with self._sessions() as db:
            members = await access.list_members(db)
            pending = await access.list_applications(db, access.PENDING)
            rejected = await access.list_applications(db, access.REJECTED)
        counts = {
            "f": sum(m.role in access.FAMILY_ROLES for m in members),
            "g": sum(m.role == "guest" for m in members),
            "p": len(pending),
            "r": len(rejected),
        }
        rows = [[_button(f"{title}: {counts[key]}", f"pls:{key}")] for key, title in SECTION_TITLES.items()]
        return "👥 Люди", InlineKeyboardMarkup(inline_keyboard=rows)

    async def _section(self, key: str) -> View:
        async with self._sessions() as db:
            members = await access.list_members(db)
            status = access.PENDING if key == "p" else access.REJECTED
            applications = await access.list_applications(db, status) if key in ("p", "r") else []
        if key == "f":
            entries = [
                (f"{m.name} · {ROLE_LABELS[m.role]}", f"pc:{m.telegram_id}")
                for m in members
                if m.role in access.FAMILY_ROLES
            ]
        elif key == "g":
            entries = [(m.name, f"pc:{m.telegram_id}") for m in members if m.role == "guest"]
        else:
            action = "app" if key == "p" else "prb"
            entries = [(a.name, f"{action}:{a.telegram_id}") for a in applications]
        rows = [[_button(label, data)] for label, data in entries[:MAX_LISTED]]
        rows.append([_button("← Назад", "pl")])
        title = SECTION_TITLES[key]
        if not entries:
            title += ": пока никого"
        elif len(entries) > MAX_LISTED:
            title += f": показаны последние {MAX_LISTED} из {len(entries)}"
        if key == "r" and entries:
            title += "\nНажми на человека, чтобы вернуть его заявку в ожидание."
        return title, InlineKeyboardMarkup(inline_keyboard=rows)

    @staticmethod
    def _back(data: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[[_button("← Назад", data)]])

    async def _member_card(self, actor: AssistantMember, telegram_id: int) -> View:
        async with self._sessions() as db:
            member = await access.get_member(db, telegram_id)
        if member is None:
            return "Этого человека уже нет в Luna.", self._back("pl")
        rows = []
        if member.role == "guest":
            rows.append([_button("👨‍👩‍👧 Перевести в семью", f"pgf:{telegram_id}")])
        if self._may_exclude(actor, member):
            rows.append([_button("⛔ Снять доступ", f"px:{telegram_id}", "danger")])
        section = "g" if member.role == "guest" else "f"
        rows.append([_button("← Назад", f"pls:{section}")])
        return f"{member.name}\nСтатус: {ROLE_LABELS[member.role]}", InlineKeyboardMarkup(inline_keyboard=rows)

    async def _family_choice(self, telegram_id: int) -> View:
        try:
            people = await self._free_people(telegram_id)
        except (PiNotFoundError, PiUnavailableError):
            return NETWORK_DOWN, self._back(f"pc:{telegram_id}")
        buttons = [_button(str(p["name"]), f"pgp:{telegram_id}:{p['id']}") for p in people]
        rows = [*_pairs(buttons), [_button("← Назад", f"pc:{telegram_id}")]]
        return "Кто это из семьи?", InlineKeyboardMarkup(inline_keyboard=rows)

    def _confirm_exclude(self, telegram_id: int) -> View:
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [_button("Да, снять доступ", f"pxy:{telegram_id}", "danger")],
                [_button("← Назад", f"pc:{telegram_id}")],
            ]
        )
        return "Снять доступ? Память этого человека будет удалена, новую заявку он подать не сможет.", markup

    async def send_summary(self, chat_id: int) -> None:
        text, markup = await self._summary()
        await self._bot.send_message(chat_id, text, reply_markup=markup)

    async def send_invite(self, chat_id: int) -> None:
        link = f"https://t.me/{self._username}"
        share = f"https://t.me/share/url?url={quote(link, safe='')}&text={quote(INVITE_TEXT, safe='')}"
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📨 Переслать", url=share)]])
        text = "Перешли приглашение. Доступ откроется, когда взрослый примет заявку."
        await self._bot.send_message(chat_id, text, reply_markup=markup)

    @staticmethod
    async def _edit(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None) -> None:
        if isinstance(callback.message, Message):
            await callback.message.edit_text(text, reply_markup=markup)

    async def handle(self, callback: CallbackQuery, actor: AssistantMember, prefix: str, args: list[str]) -> None:
        """Every action here is open to adults only; LunaBot checks that before calling."""
        if prefix == "pl":
            await self._edit(callback, *await self._summary())
        elif prefix == "pls":
            await self._edit(callback, *await self._section(args[0]))
        elif prefix == "pc":
            await self._edit(callback, *await self._member_card(actor, int(args[0])))
        elif prefix == "pgf":
            await self._edit(callback, *await self._family_choice(int(args[0])))
        elif prefix == "pgp":
            await self._guest_to_family(callback, actor, int(args[0]), int(args[1]))
        elif prefix == "px":
            await self._edit(callback, *self._confirm_exclude(int(args[0])))
        elif prefix == "pxy":
            await self._exclude(callback, actor, int(args[0]))
        elif prefix == "prb":
            await self._reopen(callback, int(args[0]))
        elif prefix == "apf":
            await self._admit_family(callback, actor, int(args[0]), int(args[1]))
        elif prefix == "apg":
            await self._admit_guest(callback, actor, int(args[0]))
        elif prefix == "apr":
            await self._reject(callback, actor, int(args[0]))
        elif prefix == "app":
            await self._refresh(callback, int(args[0]))

    async def _refresh(self, callback: CallbackQuery, applicant_id: int) -> None:
        async with self._sessions() as db:
            application = await access.get_application(db, applicant_id)
        if application is None or application.status != access.PENDING:
            await self._edit(callback, ALREADY_DECIDED, self._back("pl"))
            return
        await self._edit(callback, *await self._application_view(application))
