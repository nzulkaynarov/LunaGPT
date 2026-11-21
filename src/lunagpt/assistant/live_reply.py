"""The answer appears while the model writes it: a Telegram draft (Bot API 10.3) with a stop button.

The draft is only a preview: the finished answer is sent as a normal message, which also removes the draft.
A press on "stop" ends the wait at once and keeps what was written so far; an action the model already
started (a reminder, a list item) still finishes, so nothing is left half done.
"""

import asyncio
import logging
import random
from collections.abc import Awaitable
from contextlib import suppress

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import InputRichMessage

from lunagpt.assistant.telegram import EMBEDDED_MEDIA, MESSAGE_LIMIT, RICH_MESSAGE_LIMIT, escape_prices
from lunagpt.assistant.waiting import MAX_DRAFT_ID, Waiting

logger = logging.getLogger("lunagpt.assistant.live_reply")

DRAFT_EVERY_SECONDS = 0.8  # often enough to read as live, rare enough for Telegram's limits
STOPPED_MARK = "⏹ Остановлено."

_live: dict[tuple[int, int], "LiveReply"] = {}


def stop(chat_id: int, draft_id: int) -> bool:
    """The person pressed "stop" on a draft; False when that answer is already finished."""
    live = _live.get((chat_id, draft_id))
    if live is None:
        return False
    live.stopped.set()
    return True


class LiveReply:
    def __init__(self, bot: Bot, chat_id: int, waiting: Waiting | None = None) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._waiting = waiting
        self._rich = True
        self._changed = asyncio.Event()
        self.draft_id = random.randint(1, MAX_DRAFT_ID)
        self.text = ""
        self.stopped = asyncio.Event()

    def update(self, text: str) -> None:
        """Called by the model loop with the answer so far."""
        self.text = text
        if self._waiting is not None:
            self._waiting.answer_started()
        self._changed.set()

    async def run(self, work: Awaitable[str]) -> tuple[str, bool]:
        """Wait for the answer while showing it; (the answer, whether the person stopped it)."""
        key = (self._chat_id, self.draft_id)
        _live[key] = self
        shower = asyncio.create_task(self._show())
        answer = asyncio.ensure_future(work)
        pressed = asyncio.create_task(self.stopped.wait())
        try:
            done, _ = await asyncio.wait({answer, pressed}, return_when=asyncio.FIRST_COMPLETED)
            if answer in done:
                return answer.result(), False
            answer.cancel()
            with suppress(asyncio.CancelledError):
                await answer
            return self.text, True
        finally:
            _live.pop(key, None)
            for task in (shower, pressed):
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    async def _show(self) -> None:
        shown = ""
        while True:
            await self._changed.wait()
            self._changed.clear()
            if self.stopped.is_set():
                return
            if self.text != shown:
                shown = self.text
                await self._draft(shown)
                await asyncio.sleep(DRAFT_EVERY_SECONDS)

    async def _draft(self, text: str) -> None:
        """Rich Markdown like the final answer; a half-written answer the parser refuses goes as plain text."""
        if self._rich:
            markdown = escape_prices(EMBEDDED_MEDIA.sub("", text))[:RICH_MESSAGE_LIMIT]
            try:
                await self._bot.send_rich_message_draft(
                    self._chat_id,
                    self.draft_id,
                    InputRichMessage(markdown=markdown),
                    can_stop=True,
                    keep_on_stop=True,
                )
                return
            except TelegramBadRequest as exc:
                logger.info("rich draft refused, showing plain text: %s", exc)
                self._rich = False
            except TelegramAPIError as exc:
                logger.warning("could not show the answer draft: %s", type(exc).__name__)
                return
        try:
            await self._bot.send_message_draft(
                self._chat_id, self.draft_id, text=text[:MESSAGE_LIMIT], can_stop=True, keep_on_stop=True
            )
        except TelegramAPIError as exc:
            logger.warning("could not show the answer draft: %s", type(exc).__name__)
