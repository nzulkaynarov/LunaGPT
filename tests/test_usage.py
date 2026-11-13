"""Usage counts without text, and the owner's Sunday report."""

from datetime import date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lunagpt.assistant import usage
from lunagpt.models import AssistantMember, AssistantUsage

SUNDAY = date(2026, 11, 15)


async def _family(db_sessions: async_sessionmaker[AsyncSession]) -> None:
    async with db_sessions() as db:
        db.add_all(
            [
                AssistantMember(telegram_id=1, name="Хозяин", role="owner"),
                AssistantMember(telegram_id=2, name="Миша", role="kid"),
            ]
        )
        await db.commit()


@pytest.mark.asyncio
async def test_uses_add_up_per_day_and_feature(db_sessions: async_sessionmaker[AsyncSession]) -> None:
    await _family(db_sessions)
    await usage.record(db_sessions, 1, "chat", SUNDAY, 0.01)
    await usage.record(db_sessions, 1, "chat", SUNDAY, 0.02)
    await usage.record(db_sessions, 1, "voice", SUNDAY)
    async with db_sessions() as db:
        rows = {r.feature: r for r in (await db.execute(select(AssistantUsage))).scalars()}
    assert rows["chat"].count == 2 and rows["chat"].cost_usd == Decimal("0.03")
    assert rows["voice"].count == 1


@pytest.mark.asyncio
async def test_a_broken_database_never_reaches_the_person() -> None:
    from sqlalchemy.exc import OperationalError

    session = MagicMock()
    session.__aenter__ = AsyncMock(side_effect=OperationalError("x", {}, Exception("down")))
    session.__aexit__ = AsyncMock(return_value=None)
    await usage.record(MagicMock(return_value=session), 1, "chat", SUNDAY)


@pytest.mark.parametrize(
    ("prefix", "feature"),
    [("rm", "reminders"), ("rbt", "brief"), ("flo", "lists"), ("mem", "memory"), ("quiz", "quiz"), ("upd", None)],
)
def test_buttons_map_to_features(prefix: str, feature: str | None) -> None:
    assert usage.feature_of_prefix(prefix) == feature


@pytest.mark.asyncio
async def test_week_report_names_days_features_cost_and_what_nobody_opened(
    db_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await _family(db_sessions)
    for day in (date(2026, 11, 9), date(2026, 11, 12), SUNDAY):
        await usage.record(db_sessions, 1, "chat", day, 0.10)
    await usage.record(db_sessions, 1, "lists", SUNDAY)
    await usage.record(db_sessions, 1, "chat", date(2026, 11, 8), 5.0)  # a week earlier: left out
    async with db_sessions() as db:
        text = usage.report_text(await usage.week(db, SUNDAY), SUNDAY)
    assert "09.11–15.11" in text
    assert "• Хозяин: 3 из 7 дней — чат 3, списки 1 · $0.30" in text
    assert "• Миша: 0 из 7 дней" in text
    assert "Модели за неделю: $0.30" in text
    assert "Никто не открывал:" in text and "истории" in text and "списки" not in text.split("Никто")[1]


def test_report_goes_out_on_sunday_evening_once() -> None:
    assert usage.report_due(datetime(2026, 11, 15, 20, 5), None)
    assert not usage.report_due(datetime(2026, 11, 15, 19, 55), None)
    assert not usage.report_due(datetime(2026, 11, 14, 21, 0), None)
    assert not usage.report_due(datetime(2026, 11, 15, 21, 0), SUNDAY)


@pytest.mark.asyncio
async def test_owner_gets_the_report_once(db_sessions: async_sessionmaker[AsyncSession]) -> None:
    await _family(db_sessions)
    bot = MagicMock(send_message=AsyncMock())
    evening = datetime(2026, 11, 15, 20, 0)
    assert await usage.send_weekly_report(bot, db_sessions, evening)
    assert not await usage.send_weekly_report(bot, db_sessions, evening)
    assert bot.send_message.await_count == 1
    assert bot.send_message.await_args.args[0] == 1  # only the owner
