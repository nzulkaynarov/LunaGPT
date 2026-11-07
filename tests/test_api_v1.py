"""Unit tests for LunaGPT OpenAI-compatible v1 endpoints."""

import pytest
from httpx import ASGITransport, AsyncClient

from lunagpt.api import app
from lunagpt.schemas import ChatCompletionRequest, ChatMessage
from lunagpt.services.router import detect_criticism_and_model


@pytest.mark.asyncio
async def test_v1_models_endpoint() -> None:
    """Test GET /v1/models returns curated 3-tier chat model list without internal models."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
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
    async with AsyncClient(transport=transport, base_url="http://test") as client:
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
    async with AsyncClient(transport=transport, base_url="http://test") as client:
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
    async with AsyncClient(transport=transport, base_url="http://test") as client:
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
