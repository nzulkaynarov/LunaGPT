"""After an update Luna tells each person once, in the daytime, with a cat joke and a restart button."""

import random
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lunagpt.assistant import bot, updates
from lunagpt.models import AssistantMember


def test_the_version_follows_the_menu_and_the_news() -> None:
    adult, kid = bot.menu_layout("adult"), bot.menu_layout("kid")
    assert updates.version("adult", adult) == updates.version("owner", bot.menu_layout("owner"))
    assert updates.version("adult", adult) != updates.version("kid", kid)
    assert updates.version("adult", adult) != updates.version("adult", adult + [["🆕"]])
    assert updates.newest_news() and all("2026" not in line for line in updates.newest_news())


def test_the_notice_is_a_cat_joke_and_guests_hear_no_family_news() -> None:
    family = updates.notice("kid", random.Random(1))
    assert family.split("\n")[0] in updates.PHRASES and "Что нового:" in family
    assert updates.notice("guest", random.Random(1)) in updates.PHRASES
    assert all("мур" not in phrase.lower() for phrase in updates.PHRASES)  # see the voice decision


def test_notices_wait_for_the_daytime() -> None:
    offset = bot.settings.luna_utc_offset_hours
    assert updates.is_daytime(datetime(2026, 11, 13, 10 - offset, tzinfo=UTC))
    assert not updates.is_daytime(datetime(2026, 11, 13, (23 - offset) % 24, tzinfo=UTC))


@pytest.mark.asyncio
async def test_each_person_is_told_once_and_the_button_brings_the_menu(
    db_sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    async with db_sessions() as db:
        db.add_all(
            [
                AssistantMember(telegram_id=1, name="Камила", role="adult"),
                AssistantMember(telegram_id=2, name="Заур", role="kid"),
            ]
        )
        await db.commit()
    telegram = MagicMock(send_message=AsyncMock())
    luna = bot.LunaBot(telegram, db_sessions, AsyncMock(), AsyncMock(), MagicMock(), "luna_bot", None)
    monkeypatch.setattr(updates, "is_daytime", lambda: False)
    assert await luna.announce_update() == 0

    monkeypatch.setattr(updates, "is_daytime", lambda: True)
    assert await luna.announce_update() == 2
    first = telegram.send_message.await_args_list[0]
    assert first.kwargs["reply_markup"].inline_keyboard[0][0].callback_data == updates.UPDATE_PREFIX
    assert await luna.announce_update() == 0  # already told

    async with db_sessions() as db:
        kid = await db.scalar(select(AssistantMember).where(AssistantMember.telegram_id == 2))
    assert kid is not None
    callback = MagicMock(data="upd", answer=AsyncMock())
    callback.message = MagicMock(spec=Message, delete=AsyncMock())
    luna._send = AsyncMock()  # type: ignore[method-assign]
    await luna._restart(callback, kid)
    callback.message.delete.assert_awaited_once()
    assert luna._send.await_args.args[1] == updates.RESTARTED
    assert "⏳ Моё время" in str(luna._send.await_args.args[2])


@pytest.mark.asyncio
async def test_a_new_member_gets_no_notice_after_the_welcome(
    db_sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(updates, "is_daytime", lambda: True)
    async with db_sessions() as db:
        db.add(AssistantMember(telegram_id=3, name="Гость", role="guest"))
        await db.commit()
    telegram = MagicMock(send_message=AsyncMock())
    luna = bot.LunaBot(telegram, db_sessions, AsyncMock(), AsyncMock(), MagicMock(), "luna_bot", None)
    await luna._welcome(3, "Гость", "guest")
    assert await luna.announce_update() == 0
