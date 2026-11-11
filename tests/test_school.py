"""Timetable and tasks: reading, deadlines from the timetable, saving, reminders and the brief."""

from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lunagpt.assistant import school, school_desk
from lunagpt.assistant.reminder_desk import to_local
from lunagpt.assistant.school import TaskItem, TaskList, Timetable
from lunagpt.models import AssistantMember, AssistantReminder, AssistantSchedule, AssistantTask

NOW = datetime(2026, 11, 11, 10, 30)  # a Wednesday
TODAY = NOW.date()
DAYS = {
    "mon": ["математика", "русский"],
    "tue": ["английский", "история"],
    "thu": ["литература", "Русский язык"],
    "fri": ["математика", "информатика"],
}


def test_timetable_keeps_known_days_and_reads_day_words() -> None:
    table = Timetable.model_validate({"days": {"Monday": ["математика", " "], "вторник": ["история"], "x": ["a"]}})
    assert table.days == {"mon": ["математика"], "tue": ["история"]}


@pytest.mark.parametrize(
    ("due", "expected"), [("пятница", "fri"), ("thursday", "thu"), ("2026-11-20", "2026-11-20"), ("потом", None)]
)
def test_task_deadline_words_are_normalised(due: str, expected: str | None) -> None:
    assert TaskItem(task="дело", due=due).due == expected


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        (TaskItem(subject="Математика", task="№ 125", due="fri"), date(2026, 11, 13)),
        (TaskItem(subject="Математика", task="№ 125", due="wed"), date(2026, 11, 18)),  # said on a Wednesday
        (TaskItem(subject="русский язык", task="упр. 48"), date(2026, 11, 12)),  # next lesson: Thursday
        (TaskItem(subject="История", task="параграф 12"), date(2026, 11, 17)),
        (TaskItem(subject="Биология", task="доклад"), None),  # not in the timetable, no deadline written
        (TaskItem(task="оплатить интернет", due="2026-11-15"), date(2026, 11, 15)),
    ],
)
def test_due_date_is_written_or_the_next_lesson(item: TaskItem, expected: date | None) -> None:
    assert school.due_date(item, DAYS, TODAY) == expected


def test_reminder_comes_the_evening_before() -> None:
    assert school.reminder_time(date(2026, 11, 13), NOW) == datetime(2026, 11, 12, 18, 0)
    assert school.reminder_time(date(2026, 11, 12), datetime(2026, 11, 11, 20, 0)) == datetime(2026, 11, 11, 21, 0)
    assert school.reminder_time(date(2026, 11, 11), NOW) is None  # due today: the brief shows it


@pytest.mark.parametrize(
    ("caption", "kind"),
    [
        ("моё расписание", "schedule"),
        ("запиши домашку", "tasks"),
        ("что задали", "tasks"),
        ("дневник", "tasks"),
        ("реши задачу", None),
        ("помоги с домашкой", None),
        ("", None),
    ],
)
def test_photo_caption_decides_what_to_read(caption: str, kind: str | None) -> None:
    assert school_desk.photo_kind(caption) == kind


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("запомни расписание: пн математика", True),
        ("задали по математике № 125", True),
        ("добавь задачу оплатить интернет", True),
        ("какое у меня расписание?", False),
        ("что задали на завтра?", False),
    ],
)
def test_school_text_requests(text: str, expected: bool) -> None:
    assert school_desk.is_school_text(text) is expected


def test_planner_block_for_the_chat() -> None:
    block = school.planner_block(DAYS, [("Математика", "№ 125", date(2026, 11, 13))], TODAY)
    assert "Пн: математика, русский" in block and "- Математика: № 125 (к пт 13.11)" in block


# ---------- the desk against a real database ----------


async def _member(db_sessions: async_sessionmaker[AsyncSession], telegram_id: int = 1) -> AssistantMember:
    member = AssistantMember(telegram_id=telegram_id, name="Алим", role="kid")
    async with db_sessions() as db:
        db.add(member)
        await db.commit()
    return member


def _callback(data: str) -> MagicMock:
    callback = MagicMock(data=data, answer=AsyncMock())
    callback.message = MagicMock(spec=Message, edit_text=AsyncMock())
    return callback


def _edited(callback: MagicMock) -> str:
    return str(callback.message.edit_text.call_args.args[0])


@pytest.mark.asyncio
async def test_schedule_is_previewed_saved_used_and_deleted(db_sessions: async_sessionmaker[AsyncSession]) -> None:
    member = await _member(db_sessions)
    telegram = MagicMock(send_message=AsyncMock())
    desk = school_desk.SchoolDesk(telegram, db_sessions, AsyncMock(return_value=Timetable(days=DAYS)))
    await desk.read_schedule(member, "расписание", b"jpeg")
    preview = telegram.send_message.call_args
    assert "Пн: математика, русский" in preview.args[1] and "Сохранить?" in preview.args[1]
    async with db_sessions() as db:
        assert await db.get(AssistantSchedule, 1) is None  # nothing stored before "Сохранить"

    save = _callback("sky")
    await desk.handle(save, member, "sky", [])
    assert "Расписание сохранено" in _edited(save)
    assert await desk.timetable(1) == DAYS
    assert await desk.brief_lines(1, date(2026, 11, 13)) == ["📚 Уроки: математика, информатика"]

    remove = _callback("skx")
    await desk.handle(remove, member, "skx", [])
    assert await desk.timetable(1) == {}


@pytest.mark.asyncio
async def test_tasks_get_deadlines_reminders_and_can_be_done(
    db_sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(school_desk, "local_now", lambda: NOW)
    member = await _member(db_sessions)
    async with db_sessions() as db:
        db.add(AssistantSchedule(member_id=1, days=DAYS))
        await db.commit()
    found = TaskList(
        items=[
            TaskItem(subject="Математика", task="№ 125, 127", due="fri"),
            TaskItem(subject="Русский язык", task="упр. 48"),
            TaskItem(subject="Биология", task="доклад"),
        ]
    )
    telegram = MagicMock(send_message=AsyncMock())
    desk = school_desk.SchoolDesk(telegram, db_sessions, read_tasks=AsyncMock(return_value=found))
    await desk.read_tasks(member, "домашка", b"jpeg")
    preview = telegram.send_message.call_args.args[1]
    assert "• Математика: № 125, 127 — к пт 13.11, напомню завтра в 18:00" in preview
    assert "• Русский язык: упр. 48 — на завтра, напомню сегодня в 18:00" in preview
    assert "• Биология: доклад — без срока" in preview

    await desk.handle(_callback("tky"), member, "tky", [])
    async with db_sessions() as db:
        tasks = list(await db.scalars(select(AssistantTask).order_by(AssistantTask.id)))
        reminders = list(await db.scalars(select(AssistantReminder).order_by(AssistantReminder.due_at)))
    assert [t.due_on for t in tasks] == [date(2026, 11, 13), date(2026, 11, 12), None]
    assert [to_local(r.due_at) for r in reminders] == [datetime(2026, 11, 11, 18, 0), datetime(2026, 11, 12, 18, 0)]
    assert reminders[0].text == "📝 Завтра сдавать — Русский язык: упр. 48"

    lines = await desk.brief_lines(1, date(2026, 11, 12))
    assert "📝 Сдать сегодня: Русский язык: упр. 48" in lines and "📝 На завтра: Математика: № 125, 127" in lines
    assert "Математика: № 125, 127 (к пт 13.11)" in await desk.planner_block(1)

    done = _callback(f"tkd:{tasks[1].id}")
    await desk.handle(done, member, "tkd", [str(tasks[1].id)])
    assert "упр. 48" not in _edited(done)
    async with db_sessions() as db:
        left = list(await db.scalars(select(AssistantReminder)))
    assert [r.task_id for r in left] == [tasks[0].id]  # the done task's reminder is gone


@pytest.mark.asyncio
async def test_nothing_is_saved_when_cancelled_or_unreadable(db_sessions: async_sessionmaker[AsyncSession]) -> None:
    member = await _member(db_sessions)
    telegram = MagicMock(send_message=AsyncMock())
    desk = school_desk.SchoolDesk(
        telegram, db_sessions, AsyncMock(return_value=Timetable()), AsyncMock(return_value=None)
    )
    await desk.read_schedule(member, "расписание")
    assert telegram.send_message.call_args.args[1] == school_desk.NO_SCHEDULE
    await desk.read_tasks(member, "задали")
    assert telegram.send_message.call_args.args[1] == school_desk.NOT_READ
    stale = _callback("tky")
    await desk.handle(stale, member, "tky", [])
    assert _edited(stale) == school_desk.EXPIRED
    async with db_sessions() as db:
        assert await db.scalar(select(AssistantTask)) is None
