"""Timetable and tasks: reading, deadlines from the timetable, saving, reminders and the brief."""

from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telegram_fakes import edited, pressed

from lunagpt.assistant import reminder_desk, school, school_desk
from lunagpt.assistant.reminder_desk import to_local
from lunagpt.assistant.school import TaskItem, TaskList, Timetable
from lunagpt.assistant.telegram import DANGER, MAX_PREVIEWS, Previews
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
    return edited(callback.message)


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

    data, prefix, args = pressed(preview.kwargs["reply_markup"], "✅ Сохранить")
    save = _callback(data)
    await desk.handle(save, member, prefix, args)
    assert "Расписание сохранено" in _edited(save)
    saved = save.message.edit_text.call_args.kwargs["reply_markup"]  # a way on, not a dead end
    assert pressed(saved, "📚 Расписание")[1] == "sk" and pressed(saved, "← Мой день")[1] == "rm"
    assert await desk.timetable(1) == DAYS
    assert await desk.brief_lines(1, date(2026, 11, 13)) == ["📚 Уроки: математика, информатика"]

    _, markup = await desk.schedule_view(1)
    data, prefix, args = pressed(markup, "🗑 Удалить расписание")
    remove = _callback(data)
    await desk.handle(remove, member, prefix, args)
    asked = remove.message.edit_text.call_args
    assert asked.args[0] == school_desk.CONFIRM_DELETE  # the whole timetable is big: it asks first
    assert await desk.timetable(1) == DAYS
    confirm = asked.kwargs["reply_markup"]
    assert pressed(confirm, "← Назад")[1] == "sk"
    assert [b.style for row in confirm.inline_keyboard for b in row if b.text == "Да, удалить"] == [DANGER]
    data, prefix, args = pressed(confirm, "Да, удалить")
    await desk.handle(_callback(data), member, prefix, args)
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
    assert preview.startswith("📝 Вот что нашла:")  # nothing is stored before "Сохранить"
    assert "• Математика: № 125, 127 — к пт 13.11, напомню завтра в 18:00" in preview
    assert "• Русский язык: упр. 48 — на завтра, напомню сегодня в 18:00" in preview
    assert "• Биология: доклад — без срока" in preview

    data, prefix, args = pressed(telegram.send_message.call_args.kwargs["reply_markup"], "✅ Сохранить")
    save = _callback(data)
    await desk.handle(save, member, prefix, args)
    assert _edited(save) == "📝 Сохранила заданий: 3."
    saved = save.message.edit_text.call_args.kwargs["reply_markup"]
    assert pressed(saved, "📝 Домашка")[1] == "tk" and pressed(saved, "← Мой день")[1] == "rm"
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
    assert "упр. 48" not in _edited(done) and "Математика · № 125, 127" in _edited(done)
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


@pytest.mark.parametrize(
    ("text", "is_homework"),
    [
        ("задали параграф 5", True),
        ("домашка: упражнение 12", True),
        ("дз по математике номер 3", True),
        ("домашка не получается, помоги", False),
        ("домашка по физике, как решить задачу 4?", False),
        ("задали задачу, объясни как её решать", False),
        ("добавь дело: выучить стих", True),
        ("добавь дела по дому: помыть окна", False),  # the family's chores list, not homework
    ],
)
def test_homework_to_write_down_versus_help_with_it(text: str, is_homework: bool) -> None:
    assert school.is_task_request(text) is is_homework


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["schedule", "tasks"])
async def test_every_preview_saves_exactly_what_it_shows(
    db_sessions: async_sessionmaker[AsyncSession], kind: str
) -> None:
    member = await _member(db_sessions)
    telegram = MagicMock(send_message=AsyncMock())
    first = Timetable(days={"mon": ["математика"]})
    second = Timetable(days={"tue": ["история"]})
    tasks = [TaskList(items=[TaskItem(task="математика № 125")]), TaskList(items=[TaskItem(task="русский упр. 7")])]
    desk = school_desk.SchoolDesk(
        telegram, db_sessions, AsyncMock(side_effect=[first, second]), AsyncMock(side_effect=tasks)
    )
    read = desk.read_schedule if kind == "schedule" else desk.read_tasks
    await read(member, "запиши")
    old = telegram.send_message.call_args.kwargs["reply_markup"]
    await read(member, "запиши")
    new = telegram.send_message.call_args.kwargs["reply_markup"]

    # Two homework messages in a row: each preview's "Сохранить" saves its own, in any order.
    for markup in (old, new):
        data, prefix, args = pressed(markup, "✅ Сохранить")
        await desk.handle(_callback(data), member, prefix, args)
    async with db_sessions() as db:
        saved = [t.text for t in await db.scalars(select(AssistantTask).order_by(AssistantTask.id))]
    if kind == "schedule":
        assert await desk.timetable(1) == second.days  # the later timetable replaces the earlier one
    else:
        assert saved == ["математика № 125", "русский упр. 7"]
    again = _callback(data)
    await desk.handle(again, member, prefix, args)
    assert _edited(again) == school_desk.EXPIRED  # a preview is saved once

    data, prefix, args = pressed(old, "✖ Отмена")  # a cancel on a preview that is gone is still a cancel
    late = _callback(data)
    await desk.handle(late, member, prefix, args)
    shown = late.message.edit_text.call_args
    assert shown.args == (school_desk.CANCELLED,) and shown.kwargs["reply_markup"] is None


@pytest.mark.asyncio
async def test_a_cancelled_preview_saves_nothing(db_sessions: async_sessionmaker[AsyncSession]) -> None:
    member = await _member(db_sessions)
    telegram = MagicMock(send_message=AsyncMock())
    found = TaskList(items=[TaskItem(task="старое")])
    desk = school_desk.SchoolDesk(telegram, db_sessions, read_tasks=AsyncMock(return_value=found))
    await desk.read_tasks(member, "задали")
    markup = telegram.send_message.call_args.kwargs["reply_markup"]
    data, prefix, args = pressed(markup, "✖ Отмена")
    cancel = _callback(data)
    await desk.handle(cancel, member, prefix, args)
    assert _edited(cancel) == school_desk.CANCELLED
    data, prefix, args = pressed(markup, "✅ Сохранить")
    late = _callback(data)
    await desk.handle(late, member, prefix, args)
    assert _edited(late) == school_desk.EXPIRED
    async with db_sessions() as db:
        assert await db.scalar(select(AssistantTask)) is None


def test_a_person_keeps_only_a_few_previews() -> None:
    previews: Previews[int] = Previews()
    tokens = [previews.keep(1, n) for n in range(MAX_PREVIEWS + 1)]
    assert previews.take(1, tokens[0]) is None  # the oldest made way
    assert [previews.take(1, token) for token in tokens[1:]] == list(range(1, MAX_PREVIEWS + 1))
    assert previews.take(1, tokens[1]) is None and previews.take(2, None) is None


@pytest.mark.asyncio
async def test_done_on_a_homework_reminder_marks_the_homework_done(
    db_sessions: async_sessionmaker[AsyncSession],
) -> None:
    member = await _member(db_sessions)
    rang = datetime.now(UTC)
    async with db_sessions() as db:
        task = AssistantTask(member_id=1, subject="Математика", text="№ 125", due_on=date(2026, 11, 13))
        db.add(task)
        await db.flush()
        text = "📝 Завтра сдавать — Математика: № 125"
        ring = AssistantReminder(member_id=1, text=text, due_at=rang, done_at=rang, task_id=task.id)
        db.add(ring)
        await db.commit()
    desk = school_desk.SchoolDesk(MagicMock(send_message=AsyncMock()), db_sessions)
    reminders = reminder_desk.ReminderDesk(MagicMock(send_message=AsyncMock()), db_sessions, finish_task=desk.mark_done)

    snooze = _callback(f"rmz:{ring.id}:10")
    snooze.message.text = "🔔 📝 Завтра сдавать — Математика: № 125"
    await reminders.handle(snooze, member, "rmz", [str(ring.id), "10"])
    async with db_sessions() as db:
        later = await db.scalar(select(AssistantReminder).where(AssistantReminder.done_at.is_(None)))
    assert later is not None and later.task_id == task.id  # the later ring is still this homework's

    done = _callback(f"rmok:{later.id}")
    done.message.text = "🔔 📝 Завтра сдавать — Математика: № 125"
    await reminders.handle(done, member, "rmok", [str(later.id)])
    shown = done.message.edit_text.call_args
    assert shown.args[0] == f"✅ 🔔 📝 Завтра сдавать — Математика: № 125\n{reminder_desk.HOMEWORK_DONE}"
    assert shown.kwargs["reply_markup"] is None
    assert await desk.open_tasks(1) == []
    async with db_sessions() as db:
        assert await db.scalar(select(AssistantReminder).where(AssistantReminder.task_id == task.id)) is None
