"""FastAPI Application exposing LunaGPT Core endpoints."""

import base64
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import unquote

import httpx
from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from lunagpt.config import settings
from lunagpt.db import Base, engine, get_db_session
from lunagpt.schemas import (
    ChatCompletionRequest,
    DeleteResponse,
    DocumentIngestResponse,
    DocumentItem,
    DocumentListResponse,
    FeedbackRequest,
    FeedbackResponse,
    ModelItem,
    ModelListResponse,
    PromptRequest,
    PromptResponse,
)
from lunagpt.services.ingestion import delete_document, extract_text, ingest_document, list_documents
from lunagpt.services.memory import export_dpo_dataset, save_feedback_memory
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
    """Standard OpenAI /v1/models endpoint offering the 3-tier curated lineup."""
    return ModelListResponse(
        object="list",
        data=[
            ModelItem(id="luna-smart", owned_by="anthropic"),
            ModelItem(id="luna-fast", owned_by="google"),
            ModelItem(id="luna-pro", owned_by="anthropic"),
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
    payload = dict(req)
    payload["model"] = "luna-embed"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.litellm_master_key}",
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(settings.litellm_embeddings_endpoint, json=payload, headers=headers)
            resp.raise_for_status()
            return Response(content=resp.content, media_type="application/json")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


MAX_AUDIO_SIZE_BYTES = 25 * 1024 * 1024  # 25 MB standard OpenAI limit


@app.post("/v1/audio/transcriptions")
async def audio_transcriptions(
    file: UploadFile = File(...),
    model: str = Form("luna-fast"),
    language: str | None = Form(None),
    prompt: str | None = Form(None),
    response_format: str = Form("json"),
) -> Response:
    """OpenAI-compatible speech-to-text audio transcription powered by multimodal luna-fast."""
    try:
        content = await file.read()
        if not content:
            raise HTTPException(status_code=400, detail="Empty audio file")

        if len(content) > MAX_AUDIO_SIZE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Audio file size exceeds the 25MB limit (received {len(content) / (1024 * 1024):.1f}MB)",
            )

        filename = file.filename or "audio.ogg"
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        content_type = (file.content_type or "").lower()
        if ext in ("oga", "ogg") or "ogg" in content_type:
            audio_format = "ogg"
        elif ext in ("wav", "wave") or "wav" in content_type:
            audio_format = "wav"
        elif ext in ("mp3", "mpeg") or "mp3" in content_type or "mpeg" in content_type:
            audio_format = "mp3"
        elif ext in ("m4a", "mp4") or "mp4" in content_type or "m4a" in content_type:
            audio_format = "mp4"
        elif ext in ("webm",) or "webm" in content_type:
            audio_format = "webm"
        else:
            audio_format = "ogg"

        b64_audio = base64.b64encode(content).decode("utf-8")
        instruction = (
            "Transcribe this audio recording accurately in its spoken language. "
            "Return ONLY the verbatim transcribed text without any timestamps, explanation, or quotes."
        )
        if language:
            instruction += f" The spoken language is {language}."
        if prompt:
            instruction += f" Context/Glossary: {prompt}."

        payload = {
            "model": "luna-fast",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": b64_audio,
                                "format": audio_format,
                            },
                        },
                        {"type": "text", "text": instruction},
                    ],
                }
            ],
            "temperature": 0.0,
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings.litellm_master_key}",
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(settings.litellm_endpoint, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            transcription = data["choices"][0]["message"]["content"].strip()

            if response_format.strip().lower() == "text":
                return Response(content=transcription, media_type="text/plain; charset=utf-8")
            return Response(content=json.dumps({"text": transcription}), media_type="application/json")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Transcription failed: {exc}") from exc


@app.api_route("/process", methods=["PUT", "POST"])
async def process_external_document(request: Request) -> dict[str, Any]:
    """External document extractor endpoint compatible with Open WebUI ExternalDocumentLoader."""
    raw_filename = request.headers.get("X-Filename") or request.headers.get("x-filename") or "document.txt"
    filename = unquote(raw_filename)
    content_bytes = await request.body()
    try:
        text = extract_text(filename, content_bytes)
        if not text or not text.strip():
            text = f"[Document: {filename}]"
        return {
            "page_content": text,
            "metadata": {
                "source": filename,
                "content_type": request.headers.get("Content-Type", "application/octet-stream"),
            },
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Document extraction failed: {exc}") from exc


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


@app.get("/api/v1/documents", response_model=DocumentListResponse)
async def get_documents(db: AsyncSession = Depends(get_db_session)) -> DocumentListResponse:
    """List all ingested documents and their chunk counts."""
    docs = await list_documents(db)
    items = [DocumentItem(**d) for d in docs]
    return DocumentListResponse(documents=items, total=len(items))


@app.delete("/api/v1/documents/{document_id}", response_model=DeleteResponse)
async def remove_document(document_id: uuid.UUID, db: AsyncSession = Depends(get_db_session)) -> DeleteResponse:
    """Delete an ingested document and its chunks."""
    deleted = await delete_document(db, document_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Document not found")
    return DeleteResponse(success=True, message=f"Document {document_id} and associated chunks deleted successfully.")


@app.get("/api/v1/feedback/export-dpo")
async def export_dpo(mode: str = "dpo", db: AsyncSession = Depends(get_db_session)) -> list[dict[str, Any]]:
    """Export feedback memory pairs as training records (mode='dpo' for pairs with rejected, mode='sft' for all)."""
    try:
        require_rejected = mode.lower() != "sft"
        data: list[dict[str, Any]] = await export_dpo_dataset(db, require_rejected=require_rejected)
        return data
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
