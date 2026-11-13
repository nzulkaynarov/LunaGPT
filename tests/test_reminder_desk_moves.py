"""Reminders changed from the chat: moved to a corrected time, cancelled, listed for the model."""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lunagpt.assistant import reminder_desk, reminders
from lunagpt.models import AssistantMember, AssistantReminder

OWNER, OTHER = 1, 2


def _parts(**values: Any) -> AsyncMock:
    return AsyncMock(return_value=reminders.ReminderParts(**values))


async def _setup(db_sessions: async_sessionmaker[AsyncSession], extract: AsyncMock) -> tuple[Any, AssistantMember]:
    owner = AssistantMember(telegram_id=OWNER, name="Хозяин", role="owner")
    async with db_sessions() as db:
        db.add_all([owner, AssistantMember(telegram_id=OTHER, name="Гость", role="guest")])
        await db.commit()
    desk = reminder_desk.ReminderDesk(MagicMock(send_message=AsyncMock()), db_sessions, extract=extract)
    return desk, owner


async def _due(db_sessions: async_sessionmaker[AsyncSession]) -> datetime:
    async with db_sessions() as db:
        row = await db.scalar(select(AssistantReminder))
        assert row is not None
        local: datetime = reminder_desk.to_local(row.due_at)
        return local


@pytest.mark.asyncio
async def test_a_corrected_time_keeps_the_day(db_sessions: async_sessionmaker[AsyncSession]) -> None:
    tomorrow = reminder_desk.local_now().date() + timedelta(days=1)
    desk, owner = await _setup(db_sessions, _parts(task="собрать вещи в горы", day="tomorrow", time="08:00"))
    said = await desk.create(owner, "напомни завтра в 8 собрать вещи в горы")
    assert said.startswith("🔔 Напомню завтра в 08:00")
    assert "№1: завтра в 08:00 — собрать вещи в горы" in await desk.planner_text(OWNER)

    desk._extract = _parts(time="08:30")
    assert await desk.move(owner, 1, "в 8:30") == "Перенесено №1: напомню завтра в 08:30: собрать вещи в горы."
    assert await _due(db_sessions) == datetime.combine(tomorrow, datetime.strptime("08:30", "%H:%M").time())

    desk._extract = _parts(day="after_tomorrow", time="10:00")
    assert (await desk.move(owner, 1, "послезавтра в 10")).startswith("Перенесено №1")
    assert await _due(db_sessions) == datetime.combine(
        tomorrow + timedelta(days=1), datetime.strptime("10:00", "%H:%M").time()
    )


@pytest.mark.asyncio
async def test_nobody_moves_or_cancels_another_persons_reminder(db_sessions: async_sessionmaker[AsyncSession]) -> None:
    desk, owner = await _setup(db_sessions, _parts(task="полить цветы", day="tomorrow", time="09:00"))
    await desk.create(owner, "напомни завтра в 9 полить цветы")
    guest = AssistantMember(telegram_id=OTHER, name="Гость", role="guest")
    assert await desk.move(guest, 1, "в 10") == "Напоминания №1 нет."
    assert await desk.cancel(guest, 1) == "Напоминания №1 нет."
    desk._extract = _parts()
    assert await desk.move(owner, 1, "как-нибудь") == "Не поняла новое время."
    assert await desk.cancel(owner, 1) == "Отменено №1: полить цветы."
    assert await desk.planner_text(OWNER) == ""


@pytest.mark.asyncio
async def test_a_task_is_carried_out_by_luna_and_rings_only_if_that_fails(
    db_sessions: async_sessionmaker[AsyncSession],
) -> None:
    desk, owner = await _setup(db_sessions, _parts(task="пять новых английских слов", time="11:00", repeat="daily"))
    said = await desk.create(owner, "напомни каждый день в 11 пять новых английских слов", kind="do")
    assert said == "🗓 Сделаю сама каждый день в 11:00: пять новых английских слов"
    assert "(это задание: в это время ты выполняешь его сама)" in await desk.planner_text(OWNER)

    done: list[tuple[int, str]] = []

    async def run_task(member: AssistantMember, task: str) -> None:
        done.append((member.telegram_id, task))

    desk.run_task = run_task
    desk._bot.send_message.reset_mock()  # the confirmation card
    async with db_sessions() as db:
        row = await db.scalar(select(AssistantReminder))
        assert row is not None and row.kind == "do"
        row.due_at = datetime.now(UTC) - timedelta(minutes=1)
        await db.commit()
    assert await desk.ring_due() == 1
    assert done == [(OWNER, "пять новых английских слов")]
    desk._bot.send_message.assert_not_awaited()  # no "🔔" card: Luna did the task itself
    assert await _due(db_sessions) > reminder_desk.local_now()  # and it moved to tomorrow

    async def broken(_member: AssistantMember, _task: str) -> None:
        raise RuntimeError("model down")

    desk.run_task = broken
    async with db_sessions() as db:
        row = await db.scalar(select(AssistantReminder))
        assert row is not None
        row.due_at = datetime.now(UTC) - timedelta(minutes=1)
        await db.commit()
    assert await desk.ring_due() == 1
    assert "🔔 пять новых английских слов" in desk._bot.send_message.await_args.args[1]


@pytest.mark.asyncio
async def test_a_task_replaces_a_plain_reminder_for_the_same_time(
    db_sessions: async_sessionmaker[AsyncSession],
) -> None:
    desk, owner = await _setup(db_sessions, _parts(task="прислать пять слов", time="11:00", repeat="daily"))
    await desk.create(owner, "напомни каждый день в 11 прислать пять слов")
    desk._extract = _parts(task="пять новых слов", time="12:00", repeat="daily")
    await desk.create(owner, "напомни каждый день в 12 пять новых слов")  # another time stays
    desk._extract = _parts(task="пять новых слов", time="11:00", repeat="daily")
    said = await desk.create(owner, "напомни каждый день в 11 пять новых слов", kind="do")
    assert said.endswith("Обычное напоминание на это же время убрала.")
    async with db_sessions() as db:
        kinds = sorted(
            (r.kind, reminder_desk.to_local(r.due_at).hour) for r in await db.scalars(select(AssistantReminder))
        )
    assert kinds == [("do", 11), ("remind", 12)]
