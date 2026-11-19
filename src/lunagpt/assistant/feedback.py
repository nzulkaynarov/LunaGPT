"""A person's reaction on Luna's reply becomes a feedback score on that reply's Langfuse trace.

Telegram's own reactions, not extra buttons: the chat stays clean and a 👍 costs one tap. The trace keeps the
model, the cost, the time and the feature, never the text, so a 👎 tells the owner which model and which part of
Luna disappoint without anyone reading a family conversation."""

import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from aiogram.types import MessageReactionUpdated, ReactionTypeEmoji
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lunagpt.langfuse_api import Langfuse
from lunagpt.models import AssistantReplyTrace

logger = logging.getLogger("lunagpt.assistant.feedback")

SCORE_NAME = "user-feedback"
KEPT = timedelta(days=30)
POSITIVE = frozenset(
    [
        "👍",
        "❤",
        "❤\u200d🔥",
        "🔥",
        "🥰",
        "👏",
        "😁",
        "🤩",
        "🙏",
        "👌",
        "😍",
        "💯",
        "🤗",
        "😇",
        "🏆",
        "⚡",
        "😘",
        "🆒",
        "🫡",
        "🤝",
        "😎",
        "💘",
        "🤓",
        "✍",
    ]
)
NEGATIVE = frozenset(["👎", "💩", "🤮", "😡", "🤬", "🤡", "💔", "😢", "🤨", "😐", "🥱", "😭", "🙈", "🤷"])


def verdict(reactions: Sequence[object]) -> tuple[int | None, str]:
    """(1 for a good reply, 0 for a bad one, None for no opinion) and the emoji that decided it."""
    emojis = [r.emoji for r in reactions if isinstance(r, ReactionTypeEmoji)]
    for emoji in emojis:
        if emoji in NEGATIVE:
            return 0, emoji
    for emoji in emojis:
        if emoji in POSITIVE:
            return 1, emoji
    return None, ""


def score_id(trace_id: str) -> str:
    """One score per reply: a changed reaction replaces the first, a removed one deletes it."""
    return f"{trace_id}-feedback"


class FeedbackDesk:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], langfuse: Langfuse | None) -> None:
        self._sessions = sessions
        self._langfuse = langfuse if langfuse is not None and langfuse.enabled else None

    async def remember(self, chat_id: int, message_ids: Sequence[int], trace_id: str) -> None:
        """Called after a reply is sent; a failure only means a reaction to it will not count."""
        if self._langfuse is None or not message_ids:
            return
        rows = [{"chat_id": chat_id, "message_id": mid, "trace_id": trace_id} for mid in message_ids]
        try:
            async with self._sessions() as db:
                await db.execute(insert(AssistantReplyTrace).values(rows).on_conflict_do_nothing())
                await db.execute(
                    delete(AssistantReplyTrace).where(AssistantReplyTrace.created_at < datetime.now(UTC) - KEPT)
                )
                await db.commit()
        except Exception as exc:  # noqa: BLE001 — feedback bookkeeping must never stop a reply
            logger.warning("could not remember a reply trace: %s", type(exc).__name__)

    async def on_reaction(self, event: MessageReactionUpdated) -> None:
        if self._langfuse is None or event.user is None or event.user.is_bot:
            return
        async with self._sessions() as db:
            trace_id = await db.scalar(
                select(AssistantReplyTrace.trace_id).where(
                    AssistantReplyTrace.chat_id == event.chat.id,
                    AssistantReplyTrace.message_id == event.message_id,
                )
            )
        if trace_id is None:
            return  # a reaction on a menu, a reminder or a reply older than a month
        value, emoji = verdict(event.new_reaction)
        if value is None:
            await self._langfuse.delete_score(score_id(trace_id))
            return
        await self._langfuse.score(score_id(trace_id), trace_id, SCORE_NAME, value, emoji)
