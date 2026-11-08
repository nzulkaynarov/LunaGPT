"""Tests for mapping Open WebUI ratings to Langfuse scores."""

from typing import Any

from lunagpt.services.feedback_sync import build_rating_score, count_user_turns
from lunagpt.services.memory import openwebui_turn_trace_id

HISTORY: dict[str, Any] = {
    "u1": {"id": "u1", "role": "user", "parentId": None},
    "a1": {"id": "a1", "role": "assistant", "parentId": "u1"},
    "u2": {"id": "u2", "role": "user", "parentId": "a1"},
    "a2": {"id": "a2", "role": "assistant", "parentId": "u2"},
    "a2-regen": {"id": "a2-regen", "role": "assistant", "parentId": "u2"},
}
META = {"chat_id": "chat-1", "message_id": "a2"}


def test_count_user_turns_follows_branch() -> None:
    assert count_user_turns(HISTORY, "a1") == 1
    assert count_user_turns(HISTORY, "a2") == 2
    assert count_user_turns(HISTORY, "a2-regen") == 2
    assert count_user_turns(HISTORY, "missing") is None


def test_count_user_turns_stops_on_cycle() -> None:
    cyclic = {"a": {"id": "a", "role": "user", "parentId": "b"}, "b": {"id": "b", "role": "user", "parentId": "a"}}
    assert count_user_turns(cyclic, "a") == 2


def test_thumbs_down_links_to_turn_trace() -> None:
    result = build_rating_score({"rating": -1, "comment": "завис"}, META, HISTORY)
    assert result == (openwebui_turn_trace_id("chat-1", 2), 0.0, "завис")


def test_thumbs_up_uses_reason_when_no_comment() -> None:
    result = build_rating_score({"rating": 1, "comment": "", "reason": "accurate"}, META, HISTORY)
    assert result == (openwebui_turn_trace_id("chat-1", 2), 1.0, "accurate")


def test_unlinkable_ratings_are_skipped() -> None:
    assert build_rating_score({"rating": 0}, META, HISTORY) is None
    assert build_rating_score({"rating": 1}, {"chat_id": "chat-1"}, HISTORY) is None
    assert build_rating_score({"rating": 1}, {"chat_id": "c", "message_id": "missing"}, HISTORY) is None
