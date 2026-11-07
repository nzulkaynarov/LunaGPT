"""FastAPI Application exposing LunaGPT Core endpoints."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import Body, Depends, FastAPI, File, HTTPException, Response, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from lunagpt.config import settings
from lunagpt.db import Base, engine, get_db_session
from lunagpt.schemas import (
    ChatCompletionRequest,
    DocumentIngestResponse,
    FeedbackRequest,
    FeedbackResponse,
    ModelItem,
    ModelListResponse,
    PromptRequest,
    PromptResponse,
)
from lunagpt.services.ingestion import ingest_document
from lunagpt.services.memory import save_feedback_memory
from lunagpt.services.router import execute_openai_chat_completion, execute_smart_routing


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Ensure database tables exist on startup."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


app = FastAPI(
    title="LunaGPT Core API",
    description="Intelligent routing, document RAG, and self-improving memory engine",
    version="0.3.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok", "service": "lunagpt-core", "version": "0.3.0"}


@app.get("/v1/models", response_model=ModelListResponse)
async def list_models() -> ModelListResponse:
    """Standard OpenAI /v1/models endpoint."""
    return ModelListResponse(
        object="list",
        data=[
            ModelItem(id="luna-auto", owned_by="lunagpt"),
            ModelItem(id="luna-smart", owned_by="anthropic"),
            ModelItem(id="luna-pro", owned_by="anthropic"),
            ModelItem(id="luna-fast", owned_by="google"),
            ModelItem(id="luna-coder", owned_by="ollama"),
            ModelItem(id="luna-embed", owned_by="ollama"),
        ],
    )


@app.post("/v1/chat/completions")
async def openai_chat_completions(req: ChatCompletionRequest, db: AsyncSession = Depends(get_db_session)) -> Response:
    """OpenAI-compatible chat completions with dynamic memory and criticism escalation."""
    try:
        json_data, stream_gen = await execute_openai_chat_completion(db, req)
        if stream_gen is not None:
            return StreamingResponse(stream_gen, media_type="text/event-stream")
        return Response(content=json.dumps(json_data), media_type="application/json")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/v1/embeddings")
async def openai_embeddings(req: dict[str, Any] = Body(...)) -> Response:
    """OpenAI-compatible embeddings endpoint forwarding to LiteLLM (bge-m3)."""
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.litellm_master_key}",
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(settings.litellm_embeddings_endpoint, json=req, headers=headers)
            resp.raise_for_status()
            return Response(content=resp.content, media_type="application/json")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


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
