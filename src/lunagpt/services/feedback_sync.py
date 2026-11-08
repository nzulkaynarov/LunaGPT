"""
Service to synchronize user evaluations and feedback from Open WebUI database
into LunaGPT feedback memories (pgvector) and Langfuse traces (Level 2 & 3 Flywheel).
"""

import json
import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from lunagpt.config import settings
from lunagpt.schemas import FeedbackRequest
from lunagpt.services.memory import save_feedback_memory, send_langfuse_score

logger = logging.getLogger("lunagpt.feedback_sync")


async def sync_openwebui_feedback(db: AsyncSession) -> dict[str, Any]:
    """
    Connect to Open WebUI PostgreSQL database, fetch feedback entries that have not yet
    been vectorized into LunaGPT feedback memories, and ingest them.
    """
    ow_url = getattr(settings, "openwebui_database_url", None)
    if not ow_url:
        ow_url = settings.lunagpt_database_url.replace("/lunagpt", "/openwebui")

    synced_count = 0
    synced_ids: list[str] = []

    ow_engine = create_async_engine(ow_url, echo=False)
    try:
        async with ow_engine.connect() as ow_conn:
            table_check = await ow_conn.execute(
                text("SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='feedback'")
            )
            if not table_check.scalar():
                logger.debug("Open WebUI feedback table does not exist yet.")
                return {"synced_count": 0, "status": "no_feedback_table"}

            existing_res = await db.execute(text("SELECT trace_id FROM feedback_memories"))
            existing_traces = {r[0] for r in existing_res.fetchall() if r[0]}

            fb_res = await ow_conn.execute(
                text(
                    "SELECT id, user_id, type, data, meta, snapshot, created_at "
                    "FROM feedback ORDER BY created_at DESC LIMIT 100"
                )
            )
            rows = fb_res.fetchall()

            for row in rows:
                fb_id = str(row[0])
                data = row[3] if isinstance(row[3], dict) else (json.loads(row[3]) if row[3] else {})
                meta = row[4] if isinstance(row[4], dict) else (json.loads(row[4]) if row[4] else {})
                snapshot = row[5] if isinstance(row[5], dict) else (json.loads(row[5]) if row[5] else {})

                chat_id = meta.get("chat_id") or ""
                message_id = meta.get("message_id") or fb_id
                trace_id = meta.get("trace_id") or message_id

                if trace_id in existing_traces or fb_id in existing_traces:
                    continue

                rating_val = data.get("rating")
                is_positive = rating_val in (1, "1", "like", "positive")
                score = 1.0 if is_positive else 0.0
                comment = data.get("comment") or data.get("reason") or ""
                model_used = data.get("model_id") or meta.get("model_id") or "luna-smart"

                user_prompt = ""
                rejected_response = None

                if isinstance(snapshot, dict) and "messages" in snapshot:
                    msgs = snapshot.get("messages", [])
                    for i, m in enumerate(msgs):
                        if m.get("id") == message_id:
                            rejected_response = m.get("content")
                            if i > 0 and msgs[i - 1].get("role") == "user":
                                user_prompt = msgs[i - 1].get("content", "")
                            break

                if not user_prompt and chat_id:
                    try:
                        chat_res = await ow_conn.execute(
                            text("SELECT chat FROM chat WHERE id = :chat_id"),
                            {"chat_id": chat_id},
                        )
                        chat_row = chat_res.fetchone()
                        if chat_row and chat_row[0]:
                            chat_json = chat_row[0] if isinstance(chat_row[0], dict) else json.loads(chat_row[0])
                            history = chat_json.get("messages", []) or chat_json.get("history", {}).get("messages", {})
                            if isinstance(history, dict):
                                msg_list = list(history.values())
                            elif isinstance(history, list):
                                msg_list = history
                            else:
                                msg_list = []

                            for i, m in enumerate(msg_list):
                                if m.get("id") == message_id:
                                    rejected_response = m.get("content")
                                    for j in range(i - 1, -1, -1):
                                        if msg_list[j].get("role") == "user":
                                            user_prompt = msg_list[j].get("content", "")
                                            break
                                    break
                    except Exception as e:
                        logger.debug("Failed querying chat table for feedback: %s", e)

                if not user_prompt:
                    user_prompt = f"Open WebUI conversation {chat_id}"

                if score == 0.0:
                    corrected_response = comment or "User flagged this response as incorrect or unsatisfactory."
                    req = FeedbackRequest(
                        trace_id=trace_id,
                        session_id=chat_id,
                        user_prompt=user_prompt,
                        rejected_response=rejected_response,
                        corrected_response=corrected_response,
                        comment=comment,
                        score=score,
                        model_used=model_used,
                    )
                    await save_feedback_memory(db, req)
                else:
                    await send_langfuse_score(trace_id, 1.0, comment)

                existing_traces.add(trace_id)
                synced_ids.append(fb_id)
                synced_count += 1

    except Exception as exc:
        logger.warning("Feedback sync encountered an issue: %s", exc)
        return {"synced_count": synced_count, "error": str(exc)}
    finally:
        await ow_engine.dispose()

    return {"synced_count": synced_count, "synced_ids": synced_ids, "status": "success"}
