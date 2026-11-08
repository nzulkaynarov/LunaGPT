"""
Langfuse v2 Direct Ingestion Logger for LiteLLM Proxy.

Sends traces, generations, and token usage directly to Langfuse v2 REST API
(/api/public/ingestion) to bypass OTLP 404 incompatibilities.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
import urllib.request
import uuid
from typing import Any

from litellm.integrations.custom_logger import CustomLogger

logger = logging.getLogger("langfuse_v2_logger")
logger.setLevel(logging.INFO)


def _safe_serialize(val: Any) -> Any:
    """Recursively convert arbitrary objects to JSON-serializable primitives."""
    if isinstance(val, (str, int, float, bool)) or val is None:
        return val
    if isinstance(val, dict):
        return {str(k): _safe_serialize(v) for k, v in val.items()}
    if isinstance(val, (list, tuple, set)):
        return [_safe_serialize(item) for item in val]
    return str(val)


class LangfuseV2Logger(CustomLogger):
    """LiteLLM Custom Logger writing directly to Langfuse v2 Ingestion API."""

    def __init__(self) -> None:
        super().__init__()
        self.host = os.getenv("LANGFUSE_HOST", "http://langfuse-server:3000").rstrip("/")
        self.public_key = os.getenv("LANGFUSE_PUBLIC_KEY", "")
        self.secret_key = os.getenv("LANGFUSE_SECRET_KEY", "")
        self.ingestion_url = f"{self.host}/api/public/ingestion"

        auth_str = f"{self.public_key}:{self.secret_key}"
        self.auth_header = f"Basic {base64.b64encode(auth_str.encode('utf-8')).decode('utf-8')}"
        logger.info(f"LangfuseV2Logger initialized for host: {self.host}")

    def _send_batch(self, batch: list[dict[str, Any]]) -> None:
        if not self.public_key or not self.secret_key:
            return

        payload = {"batch": batch}
        data = json.dumps(payload, default=str).encode("utf-8")
        req = urllib.request.Request(
            self.ingestion_url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": self.auth_header,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status not in (200, 201, 207):
                    logger.warning(f"Langfuse v2 ingestion response: {resp.status}")
                else:
                    logger.info("Successfully sent trace to Langfuse v2")
        except Exception as exc:
            logger.error(f"Failed to send trace batch to Langfuse v2: {exc}")

    async def async_log_success_event(
        self, kwargs: dict[str, Any], response_obj: Any, start_time: Any, end_time: Any
    ) -> None:
        """Log successful chat/completion generation to Langfuse v2."""
        try:
            call_type = kwargs.get("call_type")
            if call_type == "embedding" or not hasattr(response_obj, "choices"):
                return
            model = kwargs.get("model") or "unknown-model"
            if "bge-m3" in model or "embed" in model:
                return

            litellm_params = kwargs.get("litellm_params", {}) or {}
            raw_metadata = litellm_params.get("metadata", {}) or {}
            metadata = _safe_serialize(raw_metadata)

            trace_id = metadata.get("trace_id") or str(uuid.uuid4())
            generation_id = str(uuid.uuid4())
            user_id = str(metadata.get("user_id") or "default-user")
            session_id = str(metadata.get("session_id") or "default-session")
            model = kwargs.get("model") or "unknown-model"

            messages = _safe_serialize(kwargs.get("messages") or [])

            output_text = ""
            usage = {}
            if hasattr(response_obj, "choices") and response_obj.choices:
                output_text = response_obj.choices[0].message.content or ""
            if hasattr(response_obj, "usage") and response_obj.usage:
                usage = {
                    "promptTokens": getattr(response_obj.usage, "prompt_tokens", 0),
                    "completionTokens": getattr(response_obj.usage, "completion_tokens", 0),
                    "totalTokens": getattr(response_obj.usage, "total_tokens", 0),
                    "unit": "TOKENS",
                }
                response_cost = kwargs.get("response_cost")
                if response_cost is not None and float(response_cost) > 0:
                    usage["totalCost"] = float(response_cost)

            start_ts = start_time.timestamp() if hasattr(start_time, "timestamp") else time.time()
            end_ts = end_time.timestamp() if hasattr(end_time, "timestamp") else time.time()
            start_iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(start_ts))
            end_iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(end_ts))

            batch: list[dict[str, Any]] = [
                {
                    "id": str(uuid.uuid4()),
                    "type": "trace-create",
                    "timestamp": start_iso,
                    "body": {
                        "id": trace_id,
                        "name": f"chat-{model}",
                        "userId": user_id,
                        "sessionId": session_id,
                        "metadata": metadata,
                    },
                },
                {
                    "id": str(uuid.uuid4()),
                    "type": "generation-create",
                    "timestamp": start_iso,
                    "body": {
                        "id": generation_id,
                        "traceId": trace_id,
                        "name": f"generation-{model}",
                        "startTime": start_iso,
                        "endTime": end_iso,
                        "model": model,
                        "input": messages,
                        "output": output_text,
                        "usage": usage,
                        "metadata": metadata,
                    },
                },
            ]
            self._send_batch(batch)
        except Exception as exc:
            logger.error(f"Error in LangfuseV2Logger async_log_success_event: {exc}")


langfuse_v2_logger = LangfuseV2Logger()
