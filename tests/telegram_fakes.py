"""A fake Telegram bot that remembers screens the way a person sees them, plain or rich."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from lunagpt.assistant.telegram import to_plain


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


def edited(message: MagicMock) -> str:
    """What a redrawn message shows now."""
    call = message.edit_text.call_args
    return str(call.args[0]) if call.args else to_plain(call.kwargs["rich_message"].html)
