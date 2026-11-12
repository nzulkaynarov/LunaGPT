"""Waiting phrases must appear only on a slow answer and must never get in the way of it."""

import asyncio
import random
from datetime import date, datetime
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
    assert (
        len(phrases) >= 2
        and len(set(phrases[:2])) == 2
        and all(p in waiting.PHRASES or p.startswith(waiting.RARE_MARK) for p in phrases)
    )
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


def test_first_phrase_fits_what_luna_is_doing() -> None:
    for kind in ("photo", "math", "voice"):
        assert waiting.Waiting(kind).next_phrase() in waiting.KIND_PHRASES[kind]
    assert waiting.Waiting("chat").next_phrase() not in sum(waiting.KIND_PHRASES.values(), ())


def test_search_is_shown_next_and_long_queries_are_cut() -> None:
    shown = waiting.Waiting("photo")
    shown.searching("цены   Hot Wheels Ташкент")
    assert shown.changed.is_set() and shown.next_phrase() == "🐾 Ищу лапкой: «цены Hot Wheels Ташкент»…"
    assert waiting.search_phrase("я" * 200).endswith("…»…") and len(waiting.search_phrase("я" * 200)) < 80


def test_time_of_day_holidays_and_family_phrases_join_the_pool() -> None:
    assert waiting.moment_phrases(datetime(2026, 11, 12, 7, 0)) == waiting.MORNING_PHRASES
    assert waiting.moment_phrases(datetime(2026, 11, 12, 14, 0)) == ()
    assert set(waiting.HOLIDAY_PHRASES["new_year"]) <= set(waiting.moment_phrases(datetime(2026, 12, 31, 23, 0)))
    assert waiting.holiday(date(2027, 3, 21)) == "navruz" and waiting.holiday(date(2027, 1, 8)) is None
    family = "🐾 Ловлю ответ за хвост…\n— автор: Заурбек"
    pool = waiting.Waiting("chat", datetime(2026, 11, 12, 14, 0), [family], random.Random(1))
    seen = {pool.next_phrase() for _ in range(200)}
    assert family in seen


def test_rare_phrases_are_rare_and_marked() -> None:
    pool = waiting.Waiting("chat", datetime(2026, 11, 12, 14, 0), rng=random.Random(7))
    shown = [pool.next_phrase() for _ in range(5000)]
    rare = [p for p in shown if p.startswith(waiting.RARE_MARK)]
    assert 40 < len(rare) < 170 and all(p.removeprefix(waiting.RARE_MARK) in waiting.RARE_PHRASES for p in rare)


@pytest.mark.usefixtures("quick_timing")
@pytest.mark.asyncio
async def test_a_search_wakes_the_cat_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(waiting, "FIRST_PHRASE_AFTER_SECONDS", 10.0)
    bot = _bot()
    async with waiting.cat_is_thinking(bot, 1) as cat:
        cat.searching("погода Ташкент")
        await asyncio.sleep(0.05)
    first = bot.send_message_draft.await_args_list[0]
    assert first.kwargs["text"] == "🐾 Ищу лапкой: «погода Ташкент»…"
