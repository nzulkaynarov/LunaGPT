"""Timetable and tasks in Telegram: read from a photo or text, confirm, list, mark done, remind the evening before."""

import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from aiogram import Bot
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lunagpt.assistant import school
from lunagpt.assistant.reminder_desk import local_now, to_utc
from lunagpt.assistant.reminders import when_text
from lunagpt.assistant.telegram import send_long
from lunagpt.models import AssistantMember, AssistantReminder, AssistantSchedule, AssistantTask

logger = logging.getLogger("lunagpt.assistant.school_desk")

SCHOOL_PREFIXES = frozenset({"sk", "skx", "sky", "skn", "tk", "tkd", "tky", "tkn"})
MAX_OPEN_TASKS = 100
LIST_LIMIT = 15
# A photo caption that asks to keep the homework; "реши задачу" or "помоги с домашкой" stay a question.
HOMEWORK_CAPTION = re.compile(r"^\s*(?:запиши|запомни|сохрани|добавь)\b|задали|задано|дневник", re.IGNORECASE)
SCHEDULE_REQUEST = re.compile(r"^\s*(?:запомни|сохрани|запиши|новое|вот|это)\b.*расписани", re.IGNORECASE | re.DOTALL)
NOT_READ = "Не получилось прочитать, попробуй ещё раз или пришли фото почётче."
NO_SCHEDULE = "Не нашла здесь расписание уроков. Пришли фото расписания или напиши его текстом."
NO_TASKS = "Не нашла здесь заданий. Напиши, например: «задали по математике № 125 к пятнице»."
EXPIRED = "Это уже неактуально, пришли ещё раз."


class TaskMirror(Protocol):
    """Somewhere tasks with a deadline are copied to, such as Google Calendar."""

    async def sync_tasks(self, user_id: int) -> int: ...

    async def remove_task(self, user_id: int, event_id: str) -> None: ...


ReadScheduleFn = Callable[..., Awaitable[school.Timetable | None]]
ReadTasksFn = Callable[..., Awaitable[school.TaskList | None]]


@dataclass(frozen=True)
class Planned:
    subject: str
    task: str
    due: date | None
    remind_at: datetime | None


def _button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _confirm(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[_button("✅ Сохранить", f"{prefix}y"), _button("✖ Отмена", f"{prefix}n")]]
    )


def is_school_text(text: str) -> bool:
    return bool(SCHEDULE_REQUEST.match(text)) or school.is_task_request(text)


def photo_kind(caption: str) -> str | None:
    """ "schedule", "tasks" or None (an ordinary photo question) by the caption."""
    if school.is_schedule_caption(caption):
        return "schedule"
    if HOMEWORK_CAPTION.search(caption):
        return "tasks"
    return None


class SchoolDesk:
    """Everything is scoped to the member who asks; a preview waits for "Сохранить" before anything is stored."""

    def __init__(
        self,
        bot: Bot,
        session_factory: async_sessionmaker[AsyncSession],
        read_schedule: ReadScheduleFn = school.read_schedule,
        read_tasks: ReadTasksFn = school.read_tasks,
        mirror: TaskMirror | None = None,
    ) -> None:
        self._bot = bot
        self._sessions = session_factory
        self._read_schedule = read_schedule
        self._read_tasks = read_tasks
        self.mirror = mirror
        self._pending_schedule: dict[int, dict[str, list[str]]] = {}
        self._pending_tasks: dict[int, list[Planned]] = {}

    @staticmethod
    def _metadata(member: AssistantMember) -> dict[str, object]:
        return {
            "trace_id": str(uuid.uuid4()),
            "user_id": f"member:{member.telegram_id}",
            "session_id": f"luna:{member.telegram_id}",
            "source": "luna_assistant",
            "tags": ["client:luna-assistant", "feature:school", f"role:{member.role}"],
        }

    # ---------- reading ----------

    async def handle_text(self, member: AssistantMember, text: str) -> None:
        if SCHEDULE_REQUEST.match(text):
            await self.read_schedule(member, text)
        else:
            await self.read_tasks(member, text)

    async def read_schedule(
        self, member: AssistantMember, text: str, image: bytes | None = None, image_type: str = "image/jpeg"
    ) -> None:
        user_id = member.telegram_id
        found = await self._read_schedule(text, self._metadata(member), image, image_type)
        if found is None:
            await send_long(self._bot, user_id, NOT_READ)
            return
        if not found.days:
            await send_long(self._bot, user_id, NO_SCHEDULE)
            return
        self._pending_schedule[user_id] = found.days
        preview = f"📚 Прочитала расписание:\n\n{school.timetable_text(found.days)}\n\nСохранить?"
        await send_long(self._bot, user_id, preview, _confirm("sk"))

    async def read_tasks(
        self, member: AssistantMember, text: str, image: bytes | None = None, image_type: str = "image/jpeg"
    ) -> None:
        user_id = member.telegram_id
        now = local_now()
        found = await self._read_tasks(text, now, self._metadata(member), image, image_type)
        if found is None:
            await send_long(self._bot, user_id, NOT_READ)
            return
        if not found.items:
            await send_long(self._bot, user_id, NO_TASKS)
            return
        days = await self.timetable(user_id)
        planned = []
        for item in found.items:
            due = school.due_date(item, days, now.date())
            remind_at = school.reminder_time(due, now) if due else None
            planned.append(Planned(item.subject.strip(), item.task.strip(), due, remind_at))
        self._pending_tasks[user_id] = planned
        lines = ["📝 Записала:\n"]
        for p in planned:
            line = f"• {school.task_line(p.subject, p.task)} — {school.due_text(p.due, now.date())}"
            if p.remind_at:
                line += f", напомню {when_text(p.remind_at, now)}"
            lines.append(line)
        if any(p.due is None for p in planned):
            lines.append("\nСрок не написан, а в расписании такого урока нет: такие задания будут в списке без даты.")
        lines.append("\nСохранить?")
        await send_long(self._bot, user_id, "\n".join(lines), _confirm("tk"))

    # ---------- saving and menus ----------

    async def handle(self, callback: CallbackQuery, member: AssistantMember, prefix: str, args: list[str]) -> None:
        user_id = member.telegram_id
        if prefix == "sky":
            await self._edit(callback, await self._save_schedule(user_id), None)
        elif prefix == "tky":
            await self._edit(callback, await self._save_tasks(user_id), None)
        elif prefix in ("skn", "tkn"):
            (self._pending_schedule if prefix == "skn" else self._pending_tasks).pop(user_id, None)
            await self._edit(callback, "Отменила, ничего не сохранила.", None)
        elif prefix == "skx":
            async with self._sessions() as db:
                await db.execute(delete(AssistantSchedule).where(AssistantSchedule.member_id == user_id))
                await db.commit()
            await self._edit(callback, *await self.schedule_view(user_id))
        elif prefix == "sk":
            await self._edit(callback, *await self.schedule_view(user_id))
        elif prefix == "tkd":
            await self._done(user_id, int(args[0]))
            await self._edit(callback, *await self.tasks_view(user_id))
        elif prefix == "tk":
            await self._edit(callback, *await self.tasks_view(user_id))

    @staticmethod
    async def _edit(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None) -> None:
        if isinstance(callback.message, Message):
            await callback.message.edit_text(text, reply_markup=markup)

    async def _save_schedule(self, user_id: int) -> str:
        days = self._pending_schedule.pop(user_id, None)
        if days is None:
            return EXPIRED
        async with self._sessions() as db:
            row = await db.get(AssistantSchedule, user_id)
            if row is None:
                db.add(AssistantSchedule(member_id=user_id, days=days))
            else:
                row.days = days
                row.updated_at = datetime.now().astimezone()
            await db.commit()
        return (
            f"📚 Расписание сохранено:\n\n{school.timetable_text(days)}\n\n"
            "Теперь спрашивай «что завтра?», а уроки на день будут в утренней сводке."
        )

    async def _save_tasks(self, user_id: int) -> str:
        planned = self._pending_tasks.pop(user_id, None)
        if planned is None:
            return EXPIRED
        async with self._sessions() as db:
            for p in planned:
                task = AssistantTask(member_id=user_id, subject=p.subject[:80], text=p.task, due_on=p.due)
                db.add(task)
                await db.flush()
                if p.remind_at is not None and p.due is not None:
                    days = (p.due - p.remind_at.date()).days
                    when = "Завтра сдавать" if days == 1 else f"Сдать {school.due_text(p.due, p.remind_at.date())}"
                    text = f"📝 {when} — {school.task_line(p.subject, p.task)}"
                    db.add(AssistantReminder(member_id=user_id, text=text, due_at=to_utc(p.remind_at), task_id=task.id))
            await db.commit()
        text = f"📝 Сохранила заданий: {len(planned)}. Список — в «🔔 Напоминания» → «📝 Задания»."
        if self.mirror is not None:
            try:
                copied = await self.mirror.sync_tasks(user_id)
            except Exception:
                logger.warning("could not copy tasks to the calendar", exc_info=True)
                copied = 0
            if copied:
                text += f"\nВ Google Calendar добавила: {copied}."
        return text

    async def _done(self, user_id: int, task_id: int) -> None:
        async with self._sessions() as db:
            task = await db.get(AssistantTask, task_id)
            if task is None or task.member_id != user_id:
                return
            task.done_at = datetime.now().astimezone()
            event_id = task.google_event_id
            await db.execute(delete(AssistantReminder).where(AssistantReminder.task_id == task_id))
            await db.commit()
        if event_id and self.mirror is not None:
            try:
                await self.mirror.remove_task(user_id, event_id)
            except Exception:
                logger.warning("could not remove a done task from the calendar", exc_info=True)

    # ---------- reading back ----------

    async def timetable(self, user_id: int) -> dict[str, list[str]]:
        async with self._sessions() as db:
            row = await db.get(AssistantSchedule, user_id)
            return dict(row.days) if row else {}

    async def open_tasks(self, user_id: int) -> list[AssistantTask]:
        async with self._sessions() as db:
            rows = await db.scalars(
                select(AssistantTask)
                .where(AssistantTask.member_id == user_id, AssistantTask.done_at.is_(None))
                .order_by(AssistantTask.due_on.asc().nulls_last(), AssistantTask.id)
                .limit(MAX_OPEN_TASKS)
            )
            return list(rows)

    async def schedule_view(self, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
        days = await self.timetable(user_id)
        back = [_button("← Напоминания", "rm")]
        if not days:
            text = "📚 Расписания пока нет.\n\nПришли фото расписания с подписью «расписание» или напиши его текстом."
            return text, InlineKeyboardMarkup(inline_keyboard=[back])
        keyboard = [[_button("🗑 Удалить расписание", "skx")], back]
        return f"📚 Расписание:\n\n{school.timetable_text(days)}", InlineKeyboardMarkup(inline_keyboard=keyboard)

    async def tasks_view(self, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
        tasks = await self.open_tasks(user_id)
        today = local_now().date()
        back = [_button("← Напоминания", "rm")]
        if not tasks:
            text = (
                "📝 Заданий нет.\n\nНапиши «задали по математике № 125 к пятнице» "
                "или пришли фото дневника с подписью «запиши домашку»."
            )
            return text, InlineKeyboardMarkup(inline_keyboard=[back])
        lines = ["📝 Задания:\n"]
        for number, task in enumerate(tasks[:LIST_LIMIT], start=1):
            lines.append(
                f"{number}. {school.task_line(task.subject, task.text)} — {school.due_text(task.due_on, today)}"
            )
        lines.append("\nНажми номер, когда сделано.")
        buttons = [_button(f"✅ {n}", f"tkd:{t.id}") for n, t in enumerate(tasks[:LIST_LIMIT], start=1)]
        keyboard = [buttons[i : i + 5] for i in range(0, len(buttons), 5)] + [back]
        return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=keyboard)

    async def planner_block(self, user_id: int) -> str:
        """The person's timetable and open tasks for the chat model; empty when there is nothing."""
        days = await self.timetable(user_id)
        tasks = await self.open_tasks(user_id)
        block: str = school.planner_block(days, [(t.subject, t.text, t.due_on) for t in tasks], local_now().date())
        return block

    async def brief_lines(self, user_id: int, today: date) -> list[str]:
        lines = []
        lessons = school.lessons_on(await self.timetable(user_id), today)
        if lessons:
            lines.append(f"📚 Уроки: {', '.join(lessons)}")
        tasks = await self.open_tasks(user_id)
        for label, offset in (("📝 Сдать сегодня", 0), ("📝 На завтра", 1)):
            due = [t for t in tasks if t.due_on is not None and (t.due_on - today).days == offset]
            if due:
                lines.append(f"{label}: " + "; ".join(school.task_line(t.subject, t.text) for t in due))
        late = [t for t in tasks if t.due_on is not None and t.due_on < today]
        if late:
            lines.append("⚠️ Просрочено: " + "; ".join(school.task_line(t.subject, t.text) for t in late))
        return lines
