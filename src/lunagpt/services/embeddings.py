"""Embedding generation service via LunaGPT Gateway."""

import httpx

from lunagpt.config import settings


async def get_embedding(text: str) -> list[float]:
    """
    Generate 1024-dimensional dense vector embedding using local bge-m3
    routed via LiteLLM Gateway (luna-embed).
    """
    cleaned = text.strip().replace("\n", " ")
    if not cleaned:
        cleaned = "empty document"

    payload = {
        "model": "luna-embed",
        "input": [cleaned],
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.litellm_master_key}",
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            settings.litellm_embeddings_endpoint, json=payload, headers=headers
        )
        resp.raise_for_status()
        data = resp.json()
        embedding: list[float] = data["data"][0]["embedding"]
        return embedding
