"""
Synchronize Open WebUI message ratings into Langfuse scores (Level 2 Flywheel).

Ratings carry only a thumbs value and an optional complaint, never a corrected answer,
so they are not written to feedback_memories: those records are injected into prompts
as corrected behavior and exported as DPO "chosen" samples.
"""

import json
import logging
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, create_async_engine

from lunagpt.config import settings
from lunagpt.models import OpenWebUIFeedbackSync
from lunagpt.services.memory import openwebui_turn_trace_id, send_langfuse_score

logger = logging.getLogger("lunagpt.feedback_sync")

SYNC_BATCH_SIZE = 100


def _as_dict(value: Any) -> dict[str, Any]:
    """Decode a JSON column that may be returned as text or as a parsed object."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        decoded = json.loads(value)
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _history_from_chat(chat: dict[str, Any]) -> dict[str, Any]:
    """Return the message tree keyed by message id from an Open WebUI chat document."""
    messages = chat.get("history", {}).get("messages", {})
    return messages if isinstance(messages, dict) else {}


def count_user_turns(history: dict[str, Any], message_id: str) -> int | None:
    """Count user messages on the branch ending at message_id, or None if it is unknown."""
    current = history.get(message_id)
    if current is None:
        return None
    turns = 0
    seen: set[str] = set()
    while current is not None and current.get("id") not in seen:
        seen.add(str(current.get("id")))
        if current.get("role") == "user":
            turns += 1
        current = history.get(current.get("parentId") or "")
    return turns


def build_rating_score(
    data: dict[str, Any], meta: dict[str, Any], history: dict[str, Any]
) -> tuple[str, float, str | None] | None:
    """Map an Open WebUI rating to (trace_id, score, comment), or None if it cannot be linked."""
    rating = data.get("rating")
    if rating not in (1, -1):
        return None
    chat_id = meta.get("chat_id")
    message_id = meta.get("message_id")
    if not chat_id or not message_id:
        return None
    user_turn = count_user_turns(history, str(message_id))
    if not user_turn:
        return None
    comment = data.get("comment") or data.get("reason") or None
    return openwebui_turn_trace_id(str(chat_id), user_turn), 1.0 if rating == 1 else 0.0, comment


async def _load_history(conn: AsyncConnection, snapshot: dict[str, Any], chat_id: str) -> dict[str, Any]:
    """Read the rated chat from the feedback snapshot, falling back to the chat table."""
    history = _history_from_chat(snapshot.get("chat", {}).get("chat", {}))
    if history or not chat_id:
        return history
    result = await conn.execute(text("SELECT chat FROM chat WHERE id = :chat_id"), {"chat_id": chat_id})
    row = result.fetchone()
    return _history_from_chat(_as_dict(row[0])) if row else {}


async def sync_openwebui_feedback(db: AsyncSession) -> dict[str, Any]:
    """Send unprocessed Open WebUI ratings to Langfuse and remember which ones were handled."""
    processed = {str(fid) for fid in (await db.execute(select(OpenWebUIFeedbackSync.feedback_id))).scalars()}

    synced_ids: list[str] = []
    skipped_ids: list[str] = []
    engine = create_async_engine(settings.openwebui_database_url, echo=False)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT id, data, meta, snapshot FROM feedback "
                    "WHERE type = 'rating' AND NOT (id = ANY(:processed)) "
                    "ORDER BY created_at LIMIT :limit"
                ),
                {"processed": list(processed), "limit": SYNC_BATCH_SIZE},
            )
            for feedback_id, raw_data, raw_meta, raw_snapshot in result.fetchall():
                meta = _as_dict(raw_meta)
                history = await _load_history(conn, _as_dict(raw_snapshot), str(meta.get("chat_id") or ""))
                score = build_rating_score(_as_dict(raw_data), meta, history)

                if score is None:
                    db.add(OpenWebUIFeedbackSync(feedback_id=str(feedback_id), trace_id=""))
                    await db.commit()
                    skipped_ids.append(str(feedback_id))
                    continue

                trace_id, value, comment = score
                if not await send_langfuse_score(trace_id, value, comment):
                    logger.warning("Langfuse rejected score for feedback %s; will retry", feedback_id)
                    continue
                db.add(OpenWebUIFeedbackSync(feedback_id=str(feedback_id), trace_id=trace_id))
                await db.commit()
                synced_ids.append(str(feedback_id))
    except Exception as exc:
        logger.warning("Open WebUI feedback sync failed: %s", exc)
        return {"status": "error", "error": str(exc), "synced_count": len(synced_ids), "synced_ids": synced_ids}
    finally:
        await engine.dispose()

    return {
        "status": "success",
        "synced_count": len(synced_ids),
        "synced_ids": synced_ids,
        "skipped_count": len(skipped_ids),
    }
