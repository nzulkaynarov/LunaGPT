"""Tests of LunaGPT Core: auth, probes and speech-to-text for HomeSec."""

from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from lunagpt.api import app
from lunagpt.config import settings

AUTH_HEADERS = {"Authorization": f"Bearer {settings.litellm_master_key}"}


@pytest.mark.asyncio
async def test_auth_enforcement() -> None:
    """Ensure protected endpoints return 401 on missing auth and 403 on invalid key."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Missing auth header -> 401
        resp_unauth = await client.post("/v1/audio/transcriptions", files={"file": ("v.ogg", b"x")})
        assert resp_unauth.status_code == 401

        # Invalid token -> 403
        resp_forbidden = await client.post(
            "/v1/audio/transcriptions",
            files={"file": ("v.ogg", b"x")},
            headers={"Authorization": "Bearer bad-key-12345"},
        )
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
