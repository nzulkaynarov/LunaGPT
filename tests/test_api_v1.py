"""Unit tests for LunaGPT OpenAI-compatible v1 endpoints."""

import pytest
from httpx import ASGITransport, AsyncClient

from lunagpt.api import app


@pytest.mark.asyncio
async def test_v1_models_endpoint() -> None:
    """Test GET /v1/models returns standard model list."""
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
