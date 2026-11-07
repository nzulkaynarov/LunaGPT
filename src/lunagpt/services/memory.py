"""Vector memory service for querying and saving feedback lessons in pgvector."""

import uuid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lunagpt.models import FeedbackMemory
from lunagpt.schemas import FeedbackRequest
from lunagpt.services.embeddings import get_embedding


async def save_feedback_memory(db: AsyncSession, req: FeedbackRequest) -> uuid.UUID:
    """Vectorize user prompt and save mistake/lesson in pgvector."""
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
    return memory.id


async def search_relevant_feedback(
    db: AsyncSession, prompt: str, limit: int = 2, threshold: float = 0.82
) -> list[FeedbackMemory]:
    """Search for past similar mistakes/corrections using cosine similarity."""
    prompt_emb = await get_embedding(prompt)

    stmt = (
        select(FeedbackMemory)
        .order_by(FeedbackMemory.prompt_embedding.cosine_distance(prompt_emb))
        .limit(limit)
    )
    result = await db.execute(stmt)
    memories = result.scalars().all()
    return list(memories)
