"""Tests for per-user and per-feature attribution in the Langfuse logger."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest


@pytest.fixture
def logger_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    stub = ModuleType("litellm.integrations.custom_logger")
    stub.CustomLogger = SimpleNamespace  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "litellm", ModuleType("litellm"))
    monkeypatch.setitem(sys.modules, "litellm.integrations", ModuleType("litellm.integrations"))
    monkeypatch.setitem(sys.modules, "litellm.integrations.custom_logger", stub)
    path = Path(__file__).parent.parent / "config" / "langfuse_v2_logger.py"
    spec = importlib.util.spec_from_file_location("langfuse_v2_logger_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_luna_headers_set_user_session_and_tags(logger_module: Any) -> None:
    metadata = {
        "headers": {
            "X-Luna-User": "person:7",
            "X-Luna-Session": "tg:42",
            "X-Luna-Client": "homesec",
            "X-Luna-Feature": "tutor",
        }
    }
    assert logger_module._resolve_attribution(metadata) == ("person:7", "tg:42", ["client:homesec", "feature:tutor"])


def test_core_metadata_keeps_existing_client_tag(logger_module: Any) -> None:
    metadata = {"user_id": "a@b.c", "session_id": "chat-1", "tags": ["client:openwebui", "feature:chat"]}
    assert logger_module._resolve_attribution(metadata) == ("a@b.c", "chat-1", ["client:openwebui", "feature:chat"])


def test_unknown_caller_falls_back_to_user_agent(logger_module: Any) -> None:
    metadata = {"headers": {"user-agent": "Anthropic/Python 0.116.0"}}
    assert logger_module._resolve_attribution(metadata) == ("default-user", "default-session", ["client:anthropic"])


def test_attribution_values_are_sanitized(logger_module: Any) -> None:
    metadata = {"headers": {"x-luna-user": "bad value\nwith/junk", "x-luna-client": "homesec"}}
    assert logger_module._resolve_attribution(metadata)[0] == "bad-value-with-junk"


def test_child_conversations_are_redacted(logger_module: Any) -> None:
    assert logger_module._should_redact_content(["client:homesec", "feature:tutor"])
    assert logger_module._should_redact_content(["client:homesec", "feature:stt"])
    assert logger_module._should_redact_content(["client:luna-assistant", "feature:chat"])
    assert not logger_module._should_redact_content(["client:homesec", "feature:orchestrator"])
    assert not logger_module._should_redact_content(["client:openwebui", "feature:chat"])
