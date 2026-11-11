"""Reminders and the morning brief in Telegram: create from a phrase, list, cancel, ring on time, snooze."""

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lunagpt.assistant import access, brief, reminders
from lunagpt.assistant.telegram import is_reply_to_prompt, send_long
from lunagpt.config import settings
from lunagpt.models import AssistantMember, AssistantReminder

logger = logging.getLogger("lunagpt.assistant.reminder_desk")

REMINDER_PREFIXES = frozenset({"rm", "rmd", "rmz", "rmok", "rb", "rbt", "rbx", "rbc"})
MAX_ACTIVE = 50
LIST_LIMIT = 10
POLL_SECONDS = 20
# A reminder rung this late is marked so: the PC with Luna was off at the time.
LATE_NOTE_AFTER = timedelta(minutes=15)
SNOOZES = (10, 60)
CITY_PROMPT = "Напиши ответом на это сообщение свой город для прогноза погоды."
HOW_TO = "Чтобы добавить, напиши или скажи: «напомни завтра в 9 позвонить бабушке»."
NOT_UNDERSTOOD = "Не поняла, когда напомнить. Напиши, например: «напомни завтра в 9 позвонить бабушке»."
NOT_PARSED = "Не получилось разобрать напоминание, попробуй ещё раз чуть позже."
TOO_MANY = f"У тебя уже {MAX_ACTIVE} напоминаний. Удали лишние в меню «🔔 Напоминания»."

ExtractFn = Callable[[str, datetime, dict[str, object]], Awaitable[reminders.ReminderParts | None]]
GeocodeFn = Callable[[str], Awaitable[brief.Place | None]]
ForecastFn = Callable[[float, float], Awaitable[brief.Forecast | None]]


def local_now() -> datetime:
    """Family wall-clock time, naive, as the rest of Luna counts it."""
    return (datetime.now(UTC) + timedelta(hours=settings.luna_utc_offset_hours)).replace(tzinfo=None)


def to_utc(local: datetime) -> datetime:
    return (local - timedelta(hours=settings.luna_utc_offset_hours)).replace(tzinfo=UTC)


def to_local(moment: datetime) -> datetime:
    return (moment.astimezone(UTC) + timedelta(hours=settings.luna_utc_offset_hours)).replace(tzinfo=None)


def _button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def ring_keyboard(reminder_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _button("⏰ +10 мин", f"rmz:{reminder_id}:10"),
                _button("⏰ +1 час", f"rmz:{reminder_id}:60"),
                _button("✅ Готово", f"rmok:{reminder_id}"),
            ]
        ]
    )


class ReminderDesk:
    """Every query is scoped to the member who asks, so nobody sees or cancels another person's reminders."""

    def __init__(
        self,
        bot: Bot,
        session_factory: async_sessionmaker[AsyncSession],
        extract: ExtractFn = reminders.extract,
        geocode: GeocodeFn = brief.find_city,
        weather: ForecastFn = brief.forecast,
    ) -> None:
        self._bot = bot
        self._sessions = session_factory
        self._extract = extract
        self._geocode = geocode
        self._weather = weather

    # ---------- creating ----------

    async def create(self, member: AssistantMember, text: str) -> None:
        user_id = member.telegram_id
        now = local_now()
        metadata: dict[str, object] = {
            "trace_id": str(uuid.uuid4()),
            "user_id": f"member:{user_id}",
            "session_id": f"luna:{user_id}",
            "source": "luna_assistant",
            "tags": ["client:luna-assistant", "feature:reminder", f"role:{member.role}"],
        }
        parts = await self._extract(text, now, metadata)
        if parts is None:
            await send_long(self._bot, user_id, NOT_PARSED)
            return
        due = reminders.resolve(parts, now)
        if due is None:
            await send_long(self._bot, user_id, NOT_UNDERSTOOD)
            return
        async with self._sessions() as db:
            active = await db.scalar(
                select(func.count())
                .select_from(AssistantReminder)
                .where(AssistantReminder.member_id == user_id, AssistantReminder.done_at.is_(None))
            )
            if (active or 0) >= MAX_ACTIVE:
                await send_long(self._bot, user_id, TOO_MANY)
                return
            row = AssistantReminder(member_id=user_id, text=parts.task, due_at=to_utc(due), repeat=parts.repeat)
            db.add(row)
            await db.commit()
            reminder_id = row.id
        what = f": {parts.task}" if parts.task else ""
        cancel = InlineKeyboardMarkup(inline_keyboard=[[_button("✖ Отменить", f"rmd:{reminder_id}")]])
        await send_long(self._bot, user_id, f"🔔 Напомню {reminders.when_text(due, now, parts.repeat)}{what}", cancel)

    # ---------- menu ----------

    async def _active(self, user_id: int) -> list[AssistantReminder]:
        async with self._sessions() as db:
            rows = await db.scalars(
                select(AssistantReminder)
                .where(AssistantReminder.member_id == user_id, AssistantReminder.done_at.is_(None))
                .order_by(AssistantReminder.due_at)
            )
            return list(rows)

    async def view(self, member: AssistantMember) -> tuple[str, InlineKeyboardMarkup]:
        rows = await self._active(member.telegram_id)
        now = local_now()
        if rows:
            lines = ["🔔 Твои напоминания:\n"]
            for number, row in enumerate(rows[:LIST_LIMIT], start=1):
                when = reminders.when_text(to_local(row.due_at), now, row.repeat)
                lines.append(f"{number}. {when} — {row.text or 'напоминание'}")
            if len(rows) > LIST_LIMIT:
                lines.append(f"…и ещё {len(rows) - LIST_LIMIT}")
            lines.append(f"\nНажми номер, чтобы удалить. {HOW_TO}")
        else:
            lines = [f"🔔 Напоминаний пока нет.\n\n{HOW_TO}"]
        lines.append("\n" + self._brief_status(member))
        numbers = [_button(f"✖ {n}", f"rmd:{row.id}") for n, row in enumerate(rows[:LIST_LIMIT], start=1)]
        keyboard = [numbers[i : i + 5] for i in range(0, len(numbers), 5)]
        keyboard.append([_button("☀️ Утренняя сводка", "rb")])
        return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def _brief_status(member: AssistantMember) -> str:
        if not member.brief_time:
            return "☀️ Утренняя сводка выключена."
        city = f", погода: {member.brief_city}" if member.brief_city else ", город для погоды не задан"
        return f"☀️ Утренняя сводка в {member.brief_time}{city}."

    def _brief_view(self, member: AssistantMember) -> tuple[str, InlineKeyboardMarkup]:
        text = (
            "☀️ Утренняя сводка: дата, погода в твоём городе и напоминания на день. "
            "Выбери время, когда её присылать.\n\n" + self._brief_status(member)
        )
        times = [
            _button(("✅ " if t == member.brief_time else "") + t, f"rbt:{t.replace(':', '')}")
            for t in brief.BRIEF_TIMES
        ]
        keyboard = [times[:3], times[3:], [_button("🏙 Город для погоды", "rbc")]]
        if member.brief_time:
            keyboard.append([_button("🔕 Выключить", "rbx")])
        keyboard.append([_button("← Напоминания", "rm")])
        return text, InlineKeyboardMarkup(inline_keyboard=keyboard)

    async def handle(self, callback: CallbackQuery, member: AssistantMember, prefix: str, args: list[str]) -> None:
        user_id = member.telegram_id
        if prefix == "rmz":
            await self._snooze(callback, user_id, int(args[0]), int(args[1]))
            return
        if prefix == "rmok":
            await self._mark_done(callback)
            return
        if prefix == "rmd":
            await self._delete(user_id, int(args[0]))
        elif prefix == "rbt":
            member = await self._update_brief(user_id, brief_time=f"{args[0][:2]}:{args[0][2:]}")
        elif prefix == "rbx":
            member = await self._update_brief(user_id, brief_time=None)
        elif prefix == "rbc":
            await self._bot.send_message(user_id, CITY_PROMPT, reply_markup=ForceReply())
            return
        if prefix in ("rb", "rbt", "rbx"):
            await self._edit(callback, *self._brief_view(member))
        else:
            await self._edit(callback, *await self.view(member))

    @staticmethod
    async def _edit(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None) -> None:
        if isinstance(callback.message, Message):
            await callback.message.edit_text(text, reply_markup=markup)

    async def _delete(self, user_id: int, reminder_id: int) -> None:
        async with self._sessions() as db:
            row = await db.get(AssistantReminder, reminder_id)
            if row is not None and row.member_id == user_id:
                await db.delete(row)
                await db.commit()

    async def _update_brief(self, user_id: int, **values: object) -> AssistantMember:
        async with self._sessions() as db:
            member = await db.get(AssistantMember, user_id)
            if member is None:
                raise LookupError(user_id)
            for key, value in values.items():
                setattr(member, key, value)
            await db.commit()
            return member

    async def _snooze(self, callback: CallbackQuery, user_id: int, reminder_id: int, minutes: int) -> None:
        if minutes not in SNOOZES:
            return
        async with self._sessions() as db:
            row = await db.get(AssistantReminder, reminder_id)
            if row is None or row.member_id != user_id:
                return
            # A snooze is a new one-off ring, so a repeating series keeps its own schedule.
            db.add(
                AssistantReminder(
                    member_id=user_id, text=row.text, due_at=datetime.now(UTC) + timedelta(minutes=minutes)
                )
            )
            await db.commit()
            text = row.text
        later = to_local(datetime.now(UTC) + timedelta(minutes=minutes))
        await self._edit(callback, f"🔔 {text or 'Напоминание'}\n⏰ Напомню ещё раз в {later:%H:%M}", None)

    async def _mark_done(self, callback: CallbackQuery) -> None:
        if isinstance(callback.message, Message):
            await callback.message.edit_text(f"✅ {callback.message.text or ''}".strip(), reply_markup=None)

    # ---------- city for the weather ----------

    @staticmethod
    def is_city_reply(message: Message) -> bool:
        return bool(is_reply_to_prompt(message, CITY_PROMPT))

    async def set_city(self, member: AssistantMember, text: str) -> None:
        place = await self._geocode(text)
        if place is None:
            await self._bot.send_message(
                member.telegram_id, f"Не нашла такой город. {CITY_PROMPT}", reply_markup=ForceReply()
            )
            return
        updated = await self._update_brief(
            member.telegram_id, brief_city=place.name, brief_lat=place.lat, brief_lon=place.lon
        )
        await send_long(self._bot, member.telegram_id, f"🏙 Город для погоды: {place.name}", None)
        await send_long(self._bot, member.telegram_id, *self._brief_view(updated))

    # ---------- ringing ----------

    async def ring_due(self) -> int:
        """Send every reminder whose time has come; returns how many rang."""
        now = datetime.now(UTC)
        async with self._sessions() as db:
            due = list(
                await db.scalars(
                    select(AssistantReminder)
                    .where(AssistantReminder.done_at.is_(None), AssistantReminder.due_at <= now)
                    .order_by(AssistantReminder.due_at)
                )
            )
            members = {m.telegram_id: m for m in await db.scalars(select(AssistantMember))}
            rings = [(row.id, row.member_id, row.text, row.due_at) for row in due]
            for row in due:
                after = reminders.next_time(to_local(row.due_at), row.repeat, to_local(now))
                # Moved or closed before sending: a Telegram failure loses one ring, never repeats it forever.
                if after is None:
                    row.done_at = now
                else:
                    row.due_at = to_utc(after)
            await db.commit()
        sent = 0
        for reminder_id, member_id, text, planned in rings:
            member = members.get(member_id)
            if member is None or access.access_ended(member):
                continue
            body = f"🔔 {text or 'Напоминание'}"
            if now - planned > LATE_NOTE_AFTER:
                body += f"\n(должна была напомнить {to_local(planned):%d.%m в %H:%M}, я была выключена)"
            try:
                await send_long(self._bot, member_id, body, ring_keyboard(reminder_id))
                sent += 1
            except TelegramAPIError as exc:
                logger.warning("could not send a reminder: %s", type(exc).__name__)
        return sent

    async def send_briefs(self) -> int:
        now = local_now()
        async with self._sessions() as db:
            members = [
                m
                for m in await db.scalars(select(AssistantMember).where(AssistantMember.brief_time.is_not(None)))
                if brief.is_due(m.brief_time, m.brief_sent_on, now) and not access.access_ended(m)
            ]
            for member in members:
                member.brief_sent_on = now.date()  # marked first: a failure skips one morning, never spams
            await db.commit()
        sent = 0
        for member in members:
            try:
                await send_long(self._bot, member.telegram_id, await self._brief_text(member, now))
                sent += 1
            except TelegramAPIError as exc:
                logger.warning("could not send a brief: %s", type(exc).__name__)
        return sent

    async def _brief_text(self, member: AssistantMember, now: datetime) -> str:
        weather = None
        if member.brief_city and member.brief_lat is not None and member.brief_lon is not None:
            found = await self._weather(member.brief_lat, member.brief_lon)
            weather = brief.weather_line(member.brief_city, found) if found else None
        end_of_day = to_utc(now.replace(hour=23, minute=59, second=59))
        async with self._sessions() as db:
            rows = await db.scalars(
                select(AssistantReminder)
                .where(
                    AssistantReminder.member_id == member.telegram_id,
                    AssistantReminder.done_at.is_(None),
                    AssistantReminder.due_at <= end_of_day,
                )
                .order_by(AssistantReminder.due_at)
            )
            today = [(to_local(row.due_at), row.text) for row in rows]
        text: str = brief.compose(member.name, now.date(), weather, today)
        return text

    async def watch(self) -> None:
        while True:
            for step in (self.ring_due, self.send_briefs):
                try:
                    await step()
                except Exception:
                    logger.exception("reminder step failed")
            await asyncio.sleep(POLL_SECONDS)
