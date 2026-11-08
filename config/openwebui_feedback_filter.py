"""
title: LunaGPT Active Learning & Feedback Loop
author: LunaGPT Team
description: Connects user ratings directly to Core API (/api/v1/feedback) and Langfuse.
version: 1.0.0
license: MIT
"""

from typing import Any

import requests
from pydantic import BaseModel, Field


class Filter:
    class Valves(BaseModel):
        lunagpt_core_url: str = Field(
            default="http://core:8010",
            description="LunaGPT Core API internal service URL",
        )
        api_key: str = Field(
            default="",
            description="LunaGPT Core Bearer Master Key for authentication (set in Open WebUI Valves)",
        )

    def __init__(self) -> None:
        self.valves = self.Valves()

    def outlet(self, body: dict[str, Any], __user__: dict[str, Any] | None = None) -> dict[str, Any]:
        """Ensure trace_id and routing metadata are preserved on outgoing assistant messages."""
        return body

    def action(
        self,
        body: dict[str, Any],
        __user__: dict[str, Any] | None = None,
        __event_emitter__: Any = None,
    ) -> None:
        """
        Triggered when a user provides feedback (thumbs up / thumbs down) in Open WebUI.
        Synchronizes score to Langfuse and records mistakes in pgvector memory.
        """
        try:
            feedback_type = body.get("feedback") or body.get("rating")
            score = 1.0 if feedback_type in ("like", "up", "positive", 1) else 0.0
            trace_id = body.get("trace_id") or body.get("message_id") or "openwebui-feedback"

            payload = {
                "trace_id": str(trace_id),
                "session_id": str(body.get("chat_id") or "openwebui-chat"),
                "user_prompt": str(body.get("prompt") or body.get("user_message") or "User query"),
                "rejected_response": str(body.get("response") or body.get("content") or "") if score == 0.0 else None,
                "corrected_response": str(body.get("comment") or body.get("response") or "Reviewed by user"),
                "comment": str(body.get("comment") or ""),
                "score": score,
                "model_used": str(body.get("model") or "luna-smart"),
            }

            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.valves.api_key}",
            }

            requests.post(
                f"{self.valves.lunagpt_core_url.rstrip('/')}/api/v1/feedback",
                json=payload,
                headers=headers,
                timeout=5.0,
            )
        except Exception:
            pass
