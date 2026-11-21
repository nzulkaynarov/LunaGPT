"""A fake Telegram bot that remembers screens the way a person sees them, plain or rich."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from aiogram.types import InlineKeyboardMarkup

from lunagpt.assistant.telegram import CANCEL, to_plain


def fake_bot() -> MagicMock:
    return MagicMock(send_message=AsyncMock(), send_rich_message=AsyncMock())


def last_sent(bot: MagicMock) -> tuple[str, Any]:
    """The newest message the bot sent, as text and its keyboard."""
    for name, args, kwargs in reversed(bot.mock_calls):
        if name == "send_message":
            return str(args[1]), kwargs.get("reply_markup")
        if name == "send_rich_message":
            return to_plain(args[1].html), kwargs.get("reply_markup")
    raise AssertionError("nothing was sent")


def prompt_cancel(markup: Any) -> str:
    """The callback of "✖ Отмена" under a "reply to this message" prompt; fails when the markup is no such prompt."""
    assert isinstance(markup, InlineKeyboardMarkup) and markup.force_reply is True
    [[button]] = markup.inline_keyboard
    assert button.text == CANCEL
    return str(button.callback_data)


def edited(message: MagicMock) -> str:
    """What a redrawn message shows now."""
    call = message.edit_text.call_args
    return str(call.args[0]) if call.args else to_plain(call.kwargs["rich_message"].html)


def pressed(markup: Any, text: str) -> tuple[str, str, list[str]]:
    """What a press on the button with this text sends: its data, then the prefix and arguments the bot routes."""
    assert isinstance(markup, InlineKeyboardMarkup)
    data = next(str(b.callback_data) for row in markup.inline_keyboard for b in row if b.text == text)
    prefix, *args = data.split(":")
    return data, prefix, args
