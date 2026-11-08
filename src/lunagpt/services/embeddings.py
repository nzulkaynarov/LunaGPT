"""Embedding generation service via LunaGPT Gateway."""

import httpx

from lunagpt.config import settings


async def get_embeddings_batch(texts: list[str], batch_size: int = 32) -> list[list[float]]:
    """
    Generate 1024-dimensional dense vector embeddings in batches using local bge-m3
    routed via LiteLLM Gateway (luna-embed).
    """
    if not texts:
        return []

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.litellm_master_key}",
    }

    all_embeddings: list[list[float]] = []

    async with httpx.AsyncClient(timeout=60.0) as client:
        for i in range(0, len(texts), batch_size):
            chunk_slice = texts[i : i + batch_size]
            cleaned_batch = [t.strip().replace("\n", " ") or "empty document" for t in chunk_slice]

            payload = {
                "model": "luna-embed",
                "input": cleaned_batch,
            }

            resp = await client.post(settings.litellm_embeddings_endpoint, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()

            # Ensure embeddings are sorted by index to preserve order
            sorted_items = sorted(data.get("data", []), key=lambda x: x.get("index", 0))
            for item in sorted_items:
                all_embeddings.append(item["embedding"])

    return all_embeddings


async def get_embedding(text: str) -> list[float]:
    """
    Generate 1024-dimensional dense vector embedding for a single text using local bge-m3
    routed via LiteLLM Gateway (luna-embed).
    """
    embeddings = await get_embeddings_batch([text], batch_size=1)
    if not embeddings:
        raise ValueError("Failed to obtain embedding from LiteLLM")
    return embeddings[0]
