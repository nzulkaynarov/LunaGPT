"""Vector memory service for querying and saving feedback lessons in pgvector."""

import base64
import time
import uuid
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lunagpt.config import settings
from lunagpt.models import FeedbackMemory
from lunagpt.schemas import FeedbackRequest
from lunagpt.services.embeddings import get_embedding


async def send_langfuse_score(trace_id: str, score: float, comment: str | None = None) -> bool:
    """Send user feedback score (0.0 or 1.0) directly to Langfuse v2 Ingestion API."""
    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        return False

    auth_str = f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}"
    auth_header = f"Basic {base64.b64encode(auth_str.encode('utf-8')).decode('utf-8')}"
    url = f"{settings.langfuse_host.rstrip('/')}/api/public/ingestion"

    batch = [
        {
            "id": str(uuid.uuid4()),
            "type": "score-create",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
            "body": {
                "id": str(uuid.uuid4()),
                "traceId": trace_id,
                "name": "user-feedback",
                "value": score,
                "comment": comment or "",
            },
        }
    ]

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                url,
                json={"batch": batch},
                headers={"Content-Type": "application/json", "Authorization": auth_header},
            )
            return resp.status_code in (200, 201, 207)
    except Exception:
        return False


async def save_feedback_memory(db: AsyncSession, req: FeedbackRequest) -> uuid.UUID:
    """Vectorize user prompt, save mistake/lesson in pgvector, and send score to Langfuse."""
    embedding = await get_embedding(req.user_prompt)

    memory = FeedbackMemory(
        id=uuid.uuid4(),
        trace_id=req.trace_id,
        session_id=req.session_id,
        user_prompt=req.user_prompt,
        rejected_response=req.rejected_response,
        corrected_response=req.corrected_response,
        feedback_comment=req.comment,
        score=req.score,
        model_used=req.model_used,
        prompt_embedding=embedding,
    )

    db.add(memory)
    await db.commit()
    await db.refresh(memory)

    # Level 2: Sync score to Langfuse trace
    await send_langfuse_score(req.trace_id, req.score, req.comment)

    return uuid.UUID(str(memory.id))


async def search_relevant_feedback(
    db: AsyncSession, prompt: str, limit: int = 2, threshold: float = 0.72
) -> list[FeedbackMemory]:
    """
    Search for past similar mistakes/corrections using cosine similarity.
    Only returns records where cosine similarity >= threshold (distance <= 1 - threshold).
    """
    prompt_emb = await get_embedding(prompt)
    dist_expr = FeedbackMemory.prompt_embedding.cosine_distance(prompt_emb)

    max_distance = 1.0 - threshold
    stmt = select(FeedbackMemory).where(dist_expr <= max_distance).order_by(dist_expr).limit(limit)
    result = await db.execute(stmt)
    memories = result.scalars().all()
    return list(memories)


async def export_dpo_dataset(db: AsyncSession) -> list[dict[str, Any]]:
    """Export feedback memory pairs as DPO training records (prompt, chosen, rejected)."""
    stmt = select(FeedbackMemory).order_by(FeedbackMemory.created_at.desc())
    result = await db.execute(stmt)
    records = result.scalars().all()
    return [
        {
            "prompt": r.user_prompt,
            "chosen": r.corrected_response,
            "rejected": r.rejected_response or "",
            "model": r.model_used,
            "score": r.score,
            "created_at": r.created_at.isoformat() if r.created_at else "",
        }
        for r in records
    ]
