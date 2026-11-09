"""Waiting phrases must appear only on a slow answer and must never get in the way of it."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest

from lunagpt.assistant import waiting


def _bot(**kwargs: Any) -> MagicMock:
    return MagicMock(send_message_draft=AsyncMock(**kwargs))


@pytest.fixture
def quick_timing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(waiting, "FIRST_PHRASE_AFTER_SECONDS", 0.01)
    monkeypatch.setattr(waiting, "NEXT_PHRASE_AFTER_SECONDS", 0.01)


@pytest.mark.asyncio
async def test_quick_answer_shows_no_phrase() -> None:
    bot = _bot()
    async with waiting.cat_is_thinking(bot, 1):
        pass
    bot.send_message_draft.assert_not_awaited()


@pytest.mark.usefixtures("quick_timing")
@pytest.mark.asyncio
async def test_slow_answer_rotates_different_phrases_in_one_draft() -> None:
    bot = _bot()
    async with waiting.cat_is_thinking(bot, 1):
        await asyncio.sleep(0.1)
    calls = bot.send_message_draft.await_args_list
    phrases = [call.kwargs["text"] for call in calls]
    assert len(phrases) >= 2 and len(set(phrases[:2])) == 2 and set(phrases) <= set(waiting.PHRASES)
    assert {call.args[1] for call in calls} != {0} and len({call.args[1] for call in calls}) == 1


@pytest.mark.usefixtures("quick_timing")
@pytest.mark.asyncio
async def test_no_phrase_after_the_answer_is_ready() -> None:
    bot = _bot()
    async with waiting.cat_is_thinking(bot, 1):
        await asyncio.sleep(0.03)
    count = bot.send_message_draft.await_count
    await asyncio.sleep(0.05)
    assert bot.send_message_draft.await_count == count


@pytest.mark.usefixtures("quick_timing")
@pytest.mark.asyncio
async def test_failed_draft_never_breaks_the_answer() -> None:
    bot = _bot(side_effect=TelegramBadRequest(MagicMock(), "drafts are unavailable"))
    async with waiting.cat_is_thinking(bot, 1):
        await asyncio.sleep(0.05)
    bot.send_message_draft.assert_awaited_once()


def test_phrases_fit_telegram_and_are_unique() -> None:
    assert len(set(waiting.PHRASES)) == len(waiting.PHRASES) and all(len(p) <= 4096 for p in waiting.PHRASES)
