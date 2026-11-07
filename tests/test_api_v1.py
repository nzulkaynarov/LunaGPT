"""Unit tests for LunaGPT OpenAI-compatible v1 endpoints."""

import pytest
from httpx import ASGITransport, AsyncClient

from lunagpt.api import app
from lunagpt.schemas import ChatCompletionRequest, ChatMessage
from lunagpt.services.router import detect_criticism_and_model


@pytest.mark.asyncio
async def test_v1_models_endpoint() -> None:
    """Test GET /v1/models returns standard chat model list without embedding models."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/models")
        assert resp.status_code == 200
        data = resp.json()
        assert data["object"] == "list"
        model_ids = [m["id"] for m in data["data"]]
        assert "luna-auto" in model_ids
        assert "luna-smart" in model_ids
        assert "luna-coder" in model_ids
        assert "luna-fast" in model_ids
        assert "luna-pro" in model_ids
        assert "luna-embed" not in model_ids  # embedding model excluded from chat list


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


def test_router_list_and_dict_content() -> None:
    """Ensure detect_criticism_and_model handles lists and dicts without crashing."""
    list_prompt = [{"type": "text", "text": "код не работает, исправь"}]
    model, reason, is_crit = detect_criticism_and_model(list_prompt)
    assert model == "luna-smart"
    assert is_crit is True

    dict_prompt = {"text": "Напиши простой скрипт"}
    model, reason, is_crit = detect_criticism_and_model(dict_prompt)
    assert model == "luna-coder"
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
