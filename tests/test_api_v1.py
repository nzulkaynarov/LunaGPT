"""Unit tests for LunaGPT OpenAI-compatible v1 endpoints."""

from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from lunagpt.api import app
from lunagpt.config import settings
from lunagpt.schemas import ChatCompletionRequest, ChatMessage
from lunagpt.services.memory import openwebui_turn_trace_id
from lunagpt.services.router import detect_criticism_and_model

AUTH_HEADERS = {"Authorization": f"Bearer {settings.litellm_master_key}"}


@pytest.mark.asyncio
async def test_auth_enforcement() -> None:
    """Ensure protected endpoints return 401 on missing auth and 403 on invalid key."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Missing auth header -> 401
        resp_unauth = await client.get("/v1/models")
        assert resp_unauth.status_code == 401

        # Invalid token -> 403
        resp_forbidden = await client.get("/v1/models", headers={"Authorization": "Bearer bad-key-12345"})
        assert resp_forbidden.status_code == 403


@pytest.mark.asyncio
async def test_ready_endpoint() -> None:
    """Test GET /ready readiness probe works without auth."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/ready")
        assert resp.status_code == 200
        data = resp.json()
        assert "database" in data
        assert "gateway" in data


@pytest.mark.asyncio
async def test_v1_models_endpoint() -> None:
    """Test GET /v1/models returns curated 3-tier chat model list without internal models."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=AUTH_HEADERS) as client:
        resp = await client.get("/v1/models")
        assert resp.status_code == 200
        data = resp.json()
        assert data["object"] == "list"
        model_ids = [m["id"] for m in data["data"]]
        assert len(model_ids) == 3
        assert "luna-smart" in model_ids
        assert "luna-fast" in model_ids
        assert "luna-pro" in model_ids
        assert "luna-coder" not in model_ids
        assert "luna-auto" not in model_ids
        assert "luna-embed" not in model_ids


@pytest.mark.asyncio
async def test_health_endpoint() -> None:
    """Test GET /health returns ok."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["service"] == "lunagpt-core"


@pytest.mark.asyncio
async def test_documents_list_endpoint() -> None:
    """Test GET /api/v1/documents returns document list structure."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=AUTH_HEADERS) as client:
        resp = await client.get("/api/v1/documents")
        assert resp.status_code == 200
        data = resp.json()
        assert "documents" in data
        assert "total" in data
        assert isinstance(data["documents"], list)


@pytest.mark.asyncio
async def test_feedback_export_dpo_endpoint() -> None:
    """Test GET /api/v1/feedback/export-dpo returns list."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=AUTH_HEADERS) as client:
        resp = await client.get("/api/v1/feedback/export-dpo")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)


def test_router_list_and_dict_content() -> None:
    """Ensure detect_criticism_and_model handles lists and dicts without crashing."""
    list_prompt = [{"type": "text", "text": "код не работает, исправь"}]
    model, reason, is_crit = detect_criticism_and_model(list_prompt)
    assert model == "luna-pro"
    assert is_crit is True

    dict_prompt = {"text": "Напиши простой скрипт"}
    model, reason, is_crit = detect_criticism_and_model(dict_prompt)
    assert model == "luna-smart"
    assert is_crit is False


def test_chat_schema_flexible_formats() -> None:
    """Ensure ChatCompletionRequest accepts dict user, tool calls, and list content."""
    req = ChatCompletionRequest(
        model="luna-auto",
        messages=[
            ChatMessage(role="user", content=[{"type": "text", "text": "test"}]),
            ChatMessage(role="assistant", content=None, tool_calls=[{"id": "call_1"}]),
        ],
        user={"name": "Admin", "id": "123", "role": "admin"},
    )
    assert len(req.messages) == 2
    assert req.user["name"] == "Admin"


@pytest.mark.asyncio
async def test_process_external_document_endpoint() -> None:
    """Test PUT /process for Open WebUI ExternalDocumentLoader compatibility."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=AUTH_HEADERS) as client:
        # 1. Plain text / Markdown with URL-encoded filename
        resp = await client.put(
            "/process",
            content="Привет, мир! Это тестовый документ.".encode(),
            headers={"X-Filename": "%D0%BE%D1%82%D1%87%D0%B5%D1%82.md", "Content-Type": "text/markdown"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "Привет, мир!" in data["page_content"]
        assert data["metadata"]["source"] == "отчет.md"

        # 2. HTML document stripping scripts and tags
        html_bytes = b"<html><head><script>alert(1)</script></head><body><h1>Title</h1><p>Body text</p></body></html>"
        resp_html = await client.put(
            "/process",
            content=html_bytes,
            headers={"X-Filename": "page.html", "Content-Type": "text/html"},
        )
        assert resp_html.status_code == 200
        html_data = resp_html.json()
        assert "Title" in html_data["page_content"]
        assert "Body text" in html_data["page_content"]
        assert "alert(1)" not in html_data["page_content"]


@pytest.mark.asyncio
async def test_audio_transcriptions_empty_file() -> None:
    """Test POST /v1/audio/transcriptions returns 400 on empty audio."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=AUTH_HEADERS) as client:
        resp = await client.post(
            "/v1/audio/transcriptions",
            files={"file": ("empty.ogg", b"", "audio/ogg")},
        )
        assert resp.status_code == 400


@pytest.mark.asyncio
async def test_audio_transcriptions_mocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test POST /v1/audio/transcriptions forwards to LiteLLM and returns transcribed text."""
    from unittest.mock import MagicMock

    orig_post = httpx.AsyncClient.post

    async def mock_post(self: Any, url: Any, *args: Any, **kwargs: Any) -> Any:
        if str(url) == settings.litellm_endpoint:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json.return_value = {
                "choices": [{"message": {"content": "Привет, поставь детям паузу на 30 минут"}}]
            }
            return mock_resp
        return await orig_post(self, url, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=AUTH_HEADERS) as client:
        # Default json format
        resp = await client.post(
            "/v1/audio/transcriptions",
            files={"file": ("voice.oga", b"fake_ogg_bytes_12345", "audio/ogg")},
            data={"language": "ru"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["text"] == "Привет, поставь детям паузу на 30 минут"

        # Explicit text format
        resp_text = await client.post(
            "/v1/audio/transcriptions",
            files={"file": ("voice.wav", b"fake_wav_bytes", "audio/wav")},
            data={"response_format": "text"},
        )
        assert resp_text.status_code == 200
        assert "text/plain" in resp_text.headers["content-type"]
        assert resp_text.text == "Привет, поставь детям паузу на 30 минут"


@pytest.mark.asyncio
async def test_audio_transcriptions_oversized_file() -> None:
    """Test POST /v1/audio/transcriptions returns 413 when file exceeds 25MB."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=AUTH_HEADERS) as client:
        large_bytes = b"0" * (26 * 1024 * 1024)
        resp = await client.post(
            "/v1/audio/transcriptions",
            files={"file": ("huge.wav", large_bytes, "audio/wav")},
        )
        assert resp.status_code == 413


@pytest.mark.asyncio
async def test_batch_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test get_embeddings_batch correctly batches and sorts embeddings."""
    from unittest.mock import MagicMock

    from lunagpt.services.embeddings import get_embeddings_batch

    orig_post = httpx.AsyncClient.post

    async def mock_post(self: Any, url: Any, *args: Any, **kwargs: Any) -> Any:
        if "embeddings" in str(url):
            payload = kwargs.get("json", {})
            inputs = payload.get("input", [])
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json.return_value = {
                "data": [{"embedding": [0.1 * i] * 10, "index": i} for i in range(len(inputs))]
            }
            return mock_resp
        return await orig_post(self, url, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    texts = [f"Text chunk {i}" for i in range(5)]
    embs = await get_embeddings_batch(texts, batch_size=2)
    assert len(embs) == 5
    assert len(embs[0]) == 10


def test_chunk_text_oversized_paragraph() -> None:
    """Test _chunk_text subdivides huge paragraphs into manageable sub-chunks."""
    from lunagpt.services.ingestion import _chunk_text

    long_para = "This is sentence one. " * 50  # ~1100 chars without line breaks
    chunks = _chunk_text(long_para, chunk_size=500)
    assert len(chunks) > 1
    for c in chunks:
        assert len(c) <= 650


@pytest.mark.asyncio
async def test_process_oversized_file() -> None:
    """Test PUT /process returns 413 when file exceeds max_upload_size_bytes."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=AUTH_HEADERS) as client:
        huge_bytes = b"0" * (51 * 1024 * 1024)
        resp = await client.put(
            "/process",
            content=huge_bytes,
            headers={"X-Filename": "huge.txt"},
        )
        assert resp.status_code == 413


@pytest.mark.asyncio
async def test_chat_completions_trace_id_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test /v1/chat/completions returns X-Trace-Id header and trace_id in json payload."""
    from unittest.mock import MagicMock

    orig_post = httpx.AsyncClient.post

    async def mock_post(self: Any, url: Any, *args: Any, **kwargs: Any) -> Any:
        if str(url) == settings.litellm_endpoint:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json.return_value = {
                "id": "chatcmpl-test",
                "choices": [{"message": {"role": "assistant", "content": "Тестовый ответ"}}],
            }
            return mock_resp
        return await orig_post(self, url, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=AUTH_HEADERS) as client:
        req_payload = {
            "model": "luna-smart",
            "messages": [{"role": "user", "content": "Привет"}],
        }
        resp = await client.post(
            "/v1/chat/completions",
            json=req_payload,
            headers={"X-OpenWebUI-User-Email": "testuser@lunagpt.local", "X-OpenWebUI-Chat-Id": "session-1234"},
        )
        assert resp.status_code == 200
        assert "X-Trace-Id" in resp.headers
        assert resp.headers["X-Trace-Id"] == openwebui_turn_trace_id("session-1234", 1)
        data = resp.json()
        assert "x_lunagpt_trace_id" in data
        assert "trace_id" in data
        assert data["trace_id"] == resp.headers["X-Trace-Id"]


@pytest.mark.asyncio
async def test_feedback_sync_openwebui_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test POST /api/v1/feedback/sync-openwebui calls sync_openwebui_feedback safely."""
    from unittest.mock import AsyncMock

    mock_sync = AsyncMock(return_value={"synced_count": 0, "status": "success"})
    monkeypatch.setattr("lunagpt.api.sync_openwebui_feedback", mock_sync)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=AUTH_HEADERS) as client:
        resp = await client.post("/api/v1/feedback/sync-openwebui")
        assert resp.status_code == 200
        assert resp.json()["status"] == "success"
