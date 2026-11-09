"""What people see: the bot's profile and how a model's answer is shown."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from lunagpt.assistant import profile, telegram


def _bot(description: str = "", short: str = "", commands: list[Any] | None = None) -> MagicMock:
    return MagicMock(
        get_my_description=AsyncMock(return_value=MagicMock(description=description)),
        get_my_short_description=AsyncMock(return_value=MagicMock(short_description=short)),
        get_my_commands=AsyncMock(return_value=commands or []),
        set_my_description=AsyncMock(),
        set_my_short_description=AsyncMock(),
        set_my_commands=AsyncMock(),
    )


def test_profile_texts_fit_telegram_limits() -> None:
    assert len(profile.DESCRIPTION) <= 512 and len(profile.SHORT_DESCRIPTION) <= 120
    assert {command.command for command in profile.COMMANDS} >= {"start", "help"}


@pytest.mark.asyncio
async def test_profile_is_written_when_it_differs() -> None:
    bot = _bot()
    await profile.apply_profile(bot)
    bot.set_my_description.assert_awaited_once_with(profile.DESCRIPTION)
    bot.set_my_short_description.assert_awaited_once_with(profile.SHORT_DESCRIPTION)
    bot.set_my_commands.assert_awaited_once_with(profile.COMMANDS)


@pytest.mark.asyncio
async def test_unchanged_profile_makes_no_writes() -> None:
    bot = _bot(profile.DESCRIPTION, profile.SHORT_DESCRIPTION, profile.COMMANDS)
    await profile.apply_profile(bot)
    bot.set_my_description.assert_not_awaited()
    bot.set_my_short_description.assert_not_awaited()
    bot.set_my_commands.assert_not_awaited()


@pytest.mark.asyncio
async def test_profile_failure_does_not_stop_the_bot() -> None:
    bot = _bot()
    bot.set_my_description = AsyncMock(side_effect=TelegramRetryAfter(MagicMock(), "flood", 60))
    await profile.apply_profile(bot)


@pytest.mark.asyncio
async def test_model_reply_goes_as_a_rich_message() -> None:
    bot = MagicMock(send_rich_message=AsyncMock())
    await telegram.send_model_reply(bot, 1, "**Жирный** и $x^2$")
    assert bot.send_rich_message.await_args.args[1].markdown == "**Жирный** и $x^2$"


@pytest.mark.parametrize(
    "raw",
    [
        "до ![кот](https://evil.example/x.jpg) после",
        'до <img src="https://evil.example/x.jpg"> после',
        '<video src="https://evil.example/x.mp4"></video>до после',
    ],
)
@pytest.mark.asyncio
async def test_model_cannot_embed_outside_media(raw: str) -> None:
    bot = MagicMock(send_rich_message=AsyncMock())
    await telegram.send_model_reply(bot, 1, raw)
    sent = bot.send_rich_message.await_args.args[1].markdown
    assert "evil.example" not in sent and "до" in sent and "после" in sent


@pytest.mark.asyncio
async def test_rejected_markdown_falls_back_to_plain_text() -> None:
    bot = MagicMock(
        send_rich_message=AsyncMock(side_effect=TelegramBadRequest(MagicMock(), "can't parse")),
        send_message=AsyncMock(),
    )
    await telegram.send_model_reply(bot, 1, "сломанный <тег> ответ")
    assert bot.send_message.await_args.args[1] == "сломанный <тег> ответ"
