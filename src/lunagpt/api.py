"""LunaGPT Core: speech-to-text for the HomeSec bot, and nothing else.

Everything else the platform once served (smart routing, document RAG, Open WebUI feedback) had
no users and was removed on 2026-11-13. This service goes away too once HomeSec sends voice notes
straight to the gateway with its own key.
"""

import base64
import json
import logging
import secrets
import uuid
from typing import Any

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from lunagpt.config import settings
from lunagpt.db import get_db_session

logger = logging.getLogger("lunagpt.api")
bearer_scheme = HTTPBearer(auto_error=False)
VERSION = "0.4.0"


async def verify_auth(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> None:
    """Validate Bearer authentication token against configured master key."""
    if not settings.require_auth:
        return

    if not credentials or not credentials.credentials:
        raise HTTPException(
            status_code=401,
            detail="Missing Authorization header. Expected 'Authorization: Bearer <token>'",
            headers={"WWW-Authenticate": "Bearer"},
        )

    valid_keys = [k for k in (settings.litellm_master_key, settings.core_api_key) if k]
    if not any(secrets.compare_digest(credentials.credentials, key) for key in valid_keys):
        raise HTTPException(
            status_code=403,
            detail="Invalid API key provided",
            headers={"WWW-Authenticate": "Bearer"},
        )


app = FastAPI(title="LunaGPT Core", description="Speech-to-text for the HomeSec bot", version=VERSION)


@app.get("/health")
async def health_check() -> dict[str, str]:
    """Fast liveness probe."""
    return {"status": "ok", "service": "lunagpt-core", "version": VERSION}


@app.get("/ready")
async def readiness_check(db: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    """Readiness: the database answers."""
    try:
        db_ok = (await db.execute(text("SELECT 1"))).scalar() == 1
    except Exception as exc:  # noqa: BLE001 — a probe reports, never raises
        logger.warning("Readiness probe DB check failed: %s", type(exc).__name__)
        db_ok = False
    return {"status": "ok" if db_ok else "degraded", "database": "connected" if db_ok else "unreachable"}


MAX_AUDIO_SIZE_BYTES = 25 * 1024 * 1024  # 25 MB standard OpenAI limit


@app.post("/v1/audio/transcriptions")
async def audio_transcriptions(
    request: Request,
    file: UploadFile = File(...),
    model: str = Form("luna-fast"),
    language: str | None = Form(None),
    prompt: str | None = Form(None),
    response_format: str = Form("json"),
    _: None = Depends(verify_auth),
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
            "metadata": {
                "trace_id": str(uuid.uuid4()),
                "user_id": request.headers.get("x-luna-user") or "default-user",
                "session_id": request.headers.get("x-luna-session") or "default-session",
                "source": "lunagpt_core_stt",
                "tags": [f"client:{request.headers.get('x-luna-client') or 'api'}", "feature:stt"],
            },
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
