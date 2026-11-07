"""FastAPI Application exposing LunaGPT Core endpoints."""

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from lunagpt.db import Base, engine, get_db_session
from lunagpt.schemas import (
    DocumentIngestResponse,
    FeedbackRequest,
    FeedbackResponse,
    PromptRequest,
    PromptResponse,
)
from lunagpt.services.ingestion import ingest_document
from lunagpt.services.memory import save_feedback_memory
from lunagpt.services.router import execute_smart_routing

app = FastAPI(
    title="LunaGPT Core API",
    description="Intelligent routing, document RAG, and self-improving memory engine",
    version="0.2.0",
)


@app.on_event("startup")
async def startup_event() -> None:
    """Ensure database tables exist on startup."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@app.get("/health")
async def health_check() -> dict:
    """Health check endpoint."""
    return {"status": "ok", "service": "lunagpt-core", "version": "0.2.0"}


@app.post("/api/v1/chat", response_model=PromptResponse)
async def chat_endpoint(req: PromptRequest, db: AsyncSession = Depends(get_db_session)) -> PromptResponse:
    """Execute routed chat prompt with memory and RAG injection."""
    try:
        return await execute_smart_routing(db, req)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/v1/feedback", response_model=FeedbackResponse)
async def feedback_endpoint(req: FeedbackRequest, db: AsyncSession = Depends(get_db_session)) -> FeedbackResponse:
    """Save user correction/mistake to pgvector memory."""
    try:
        mem_id = await save_feedback_memory(db, req)
        return FeedbackResponse(
            success=True,
            memory_id=str(mem_id),
            message="Feedback and correction successfully vectorized and stored in pgvector.",
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/v1/documents/ingest", response_model=DocumentIngestResponse)
async def ingest_document_endpoint(
    file: UploadFile = File(...), db: AsyncSession = Depends(get_db_session)
) -> DocumentIngestResponse:
    """Ingest document (PDF, TXT, MD), compute embeddings, and store in pgvector."""
    try:
        content = await file.read()
        doc_id, chunks_count, duration = await ingest_document(db, file.filename or "unknown.txt", content)
        return DocumentIngestResponse(
            document_id=str(doc_id),
            title=file.filename or "unknown.txt",
            file_type=file.filename.split(".")[-1] if file.filename else "txt",
            chunks_count=chunks_count,
            duration_sec=duration,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
