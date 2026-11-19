"""A small client for the public API of the self-hosted Langfuse v2 (2.95).

The official Python SDK v3 and v4 need a Langfuse server 3.63 or newer and the SDK v2 is retired, so the few
calls Luna needs go over the documented REST API: scores, datasets, dataset runs and reading generations.
Every call fails soft: observability must never stop a reply, a reminder or the weekly report."""

import logging
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import httpx

from lunagpt.config import settings

logger = logging.getLogger("lunagpt.langfuse")

PAGE_LIMIT = 100
MAX_PAGES = 50  # 5000 generations a week is far above the family's use; the cap keeps a bug from looping


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


class Langfuse:
    def __init__(
        self,
        host: str | None = None,
        public_key: str | None = None,
        secret_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # None reads the settings; an empty string given on purpose keeps Langfuse off
        self._host = (settings.langfuse_host if host is None else host).rstrip("/")
        self._auth = (
            settings.langfuse_public_key if public_key is None else public_key,
            settings.langfuse_secret_key if secret_key is None else secret_key,
        )
        self._transport = transport

    @property
    def enabled(self) -> bool:
        return bool(self._host and all(self._auth))

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self._host, auth=self._auth, timeout=15, transport=self._transport)

    async def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        """The decoded JSON, or None when Langfuse is off, down or refuses."""
        if not self.enabled:
            return None
        try:
            async with self._client() as client:
                response = await client.request(method, path, **kwargs)
                response.raise_for_status()
                return response.json() if response.content else {}
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("langfuse %s %s failed: %s", method, path, type(exc).__name__)
            return None

    async def score(self, score_id: str, trace_id: str, name: str, value: float, comment: str | None = None) -> bool:
        """Create or replace a score: the same id overwrites, so a changed reaction is one score, not two."""
        event = {
            "id": score_id,
            "type": "score-create",
            "timestamp": _iso(datetime.now().astimezone()),
            "body": {"id": score_id, "traceId": trace_id, "name": name, "value": value, "comment": comment},
        }
        return await self._call("POST", "/api/public/ingestion", json={"batch": [event]}) is not None

    async def delete_score(self, score_id: str) -> bool:
        return await self._call("DELETE", f"/api/public/scores/{score_id}") is not None

    async def ensure_dataset(self, name: str, description: str) -> bool:
        return (
            await self._call("POST", "/api/public/datasets", json={"name": name, "description": description})
            is not None
        )

    async def upsert_item(self, dataset: str, item_id: str, input: Any, expected: Any) -> bool:
        body = {"datasetName": dataset, "id": item_id, "input": input, "expectedOutput": expected}
        return await self._call("POST", "/api/public/dataset-items", json=body) is not None

    async def link_run_item(self, run: str, item_id: str, trace_id: str, metadata: dict[str, Any]) -> bool:
        body = {"runName": run, "datasetItemId": item_id, "traceId": trace_id, "metadata": metadata}
        return await self._call("POST", "/api/public/dataset-run-items", json=body) is not None

    async def trace(self, trace_id: str, name: str, input: Any, output: Any, tags: list[str]) -> bool:
        """A trace of our own, for an evaluation step whose model calls already carry this trace id."""
        event = {
            "id": f"{trace_id}-run",
            "type": "trace-create",
            "timestamp": _iso(datetime.now().astimezone()),
            "body": {"id": trace_id, "name": name, "input": input, "output": output, "tags": tags},
        }
        return await self._call("POST", "/api/public/ingestion", json={"batch": [event]}) is not None

    async def _pages(self, path: str, params: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        for page in range(1, MAX_PAGES + 1):
            data = await self._call("GET", path, params={**params, "page": page, "limit": PAGE_LIMIT})
            if not data:
                return
            for row in data.get("data", []):
                yield row
            if page >= int(data.get("meta", {}).get("totalPages") or 0):
                return

    async def generations(self, since: datetime) -> list[dict[str, Any]] | None:
        """Every model call since `since`, or None when Langfuse cannot be read."""
        if await self._call("GET", "/api/public/health") is None:
            return None
        params = {"type": "GENERATION", "fromStartTime": _iso(since)}
        return [row async for row in self._pages("/api/public/observations", params)]

    async def scores(self, name: str, since: datetime) -> list[dict[str, Any]] | None:
        if await self._call("GET", "/api/public/health") is None:
            return None
        return [row async for row in self._pages("/api/public/scores", {"name": name, "fromTimestamp": _iso(since)})]
