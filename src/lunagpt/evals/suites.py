"""The suites: for each case, what Luna's parser is asked and how its answer is judged.

A check returns (passed, note); the note says what was wrong, so a failed case in Langfuse explains itself."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from lunagpt.assistant import calendar_desk, lists, llm, memories, reminders
from lunagpt.config import settings
from lunagpt.evals import cases

Task = Callable[[dict[str, Any], dict[str, Any]], Awaitable[Any]]
Check = Callable[[Any, dict[str, Any]], tuple[bool, str]]


@dataclass(frozen=True)
class Case:
    id: str
    input: dict[str, Any]
    expected: dict[str, Any]


@dataclass(frozen=True)
class Suite:
    name: str
    description: str
    cases: tuple[Case, ...]
    task: Task
    check: Check


def _today_text() -> str:
    return f"{cases.NOW:%Y-%m-%d}, {llm.WEEKDAY_NAMES[cases.NOW.weekday()]}"


async def _memory_task(given: dict[str, Any], metadata: dict[str, Any]) -> list[str]:
    facts: list[str] = await memories.extract_facts(str(given["text"]), _today_text(), metadata)
    return facts


def _memory_check(facts: list[str], expected: dict[str, Any]) -> tuple[bool, str]:
    text = " ".join(facts).lower()
    if not expected["required"]:
        return (not facts, "" if not facts else f"stored a fact that is not there: {facts}")
    missing = [options for options in expected["required"] if not any(o.lower() in text for o in options)]
    if missing:
        return False, f"missing {missing[0]} in {facts}"
    found = [word for word in expected["forbidden"] if word.lower() in text]
    return (not found, f"kept {found[0]!r} in {facts}" if found else "")


async def _reminder_task(given: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any] | None:
    parts = await reminders.extract(given["text"], cases.NOW, metadata)
    if parts is None:
        return None
    due = reminders.resolve(parts, cases.NOW)
    return {"task": parts.task, "repeat": parts.repeat, "due": f"{due:%Y-%m-%d %H:%M}" if due else None}


def _reminder_check(heard: dict[str, Any] | None, expected: dict[str, Any]) -> tuple[bool, str]:
    if heard is None or heard["due"] is None:
        return False, f"no time understood: {heard}"
    if not any(heard["due"].startswith(moment) for moment in expected["due"]):
        return False, f"due {heard['due']}, expected {expected['due']}"
    if heard["repeat"] != expected["repeat"]:
        return False, f"repeat {heard['repeat']}, expected {expected['repeat']}"
    if expected["task"] not in heard["task"].lower():
        return False, f"task {heard['task']!r} lost {expected['task']!r}"
    return True, ""


async def _calendar_task(given: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any] | None:
    parts = await calendar_desk.extract_event(given["text"], cases.NOW, metadata)
    planned = calendar_desk.plan(parts, cases.NOW) if parts else None
    if planned is None:
        return None
    start = f"{planned.start:%Y-%m-%d}" if planned.all_day else f"{planned.start:%Y-%m-%d %H:%M}"
    minutes = None if planned.all_day else int((planned.end - planned.start) / timedelta(minutes=1))
    return {"title": planned.title, "start": start, "minutes": minutes}


def _calendar_check(event: dict[str, Any] | None, expected: dict[str, Any]) -> tuple[bool, str]:
    if event is None:
        return False, "no event understood"
    if event["start"] != expected["start"]:
        return False, f"start {event['start']}, expected {expected['start']}"
    if expected["minutes"] is not None and event["minutes"] != expected["minutes"]:
        return False, f"{event['minutes']} minutes, expected {expected['minutes']}"
    if expected["title"].lower() not in event["title"].lower():
        return False, f"title {event['title']!r} lost {expected['title']!r}"
    return True, ""


async def _list_task(given: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any] | None:
    request = await lists.parse_request(given["text"], cases.FAMILY_LISTS, cases.PERSONAL_LISTS, metadata)
    if request is None:
        return None
    return {"action": request.action, "list": request.name, "personal": request.personal, "items": request.items}


def _list_check(heard: dict[str, Any] | None, expected: dict[str, Any]) -> tuple[bool, str]:
    if expected["action"] is None:
        return (heard is None, f"took it as a list request: {heard}" if heard else "")
    if heard is None:
        return False, "not understood as a list request"
    for key in ("action", "list", "personal"):
        if heard[key] != expected[key]:
            return False, f"{key} {heard[key]!r}, expected {expected[key]!r}"
    joined = " ".join(heard["items"]).lower()
    lost = [item for item in expected["items"] if item not in joined]
    return (not lost, f"items {heard['items']} lost {lost}" if lost else "")


SUITES = (
    Suite(
        "memory",
        "Which facts Luna keeps from a message: calendar dates for relative ones, nothing from questions or "
        "text she is asked to translate or correct.",
        tuple(
            Case(cid, {"text": text}, {"required": required, "forbidden": forbidden})
            for cid, text, required, forbidden in cases.MEMORY
        ),
        _memory_task,
        _memory_check,
    ),
    Suite(
        "reminders",
        "When a reminder rings, how often, and that the task keeps its words.",
        tuple(
            Case(cid, {"text": text}, {"due": due, "repeat": repeat, "task": task})
            for cid, text, due, repeat, task in cases.REMINDERS
        ),
        _reminder_task,
        _reminder_check,
    ),
    Suite(
        "calendar",
        "The start, length and title of a calendar event from a phrase.",
        tuple(
            Case(cid, {"text": text}, {"start": start, "minutes": minutes, "title": title})
            for cid, text, start, minutes, title in cases.CALENDAR
        ),
        _calendar_task,
        _calendar_check,
    ),
    Suite(
        "lists",
        "Which list a message changes and how; advice and requests to compose a list are not list changes.",
        tuple(
            Case(cid, {"text": text}, {"action": action, "list": name, "personal": personal, "items": items})
            for cid, text, action, name, personal, items in cases.LISTS
        ),
        _list_task,
        _list_check,
    ),
)


def run_name(source: str, now: datetime | None = None) -> str:
    """The family's wall clock: the container runs in UTC, the owner reads the runs in Tashkent time."""
    local = now or datetime.now(UTC) + timedelta(hours=settings.luna_utc_offset_hours)
    return f"{local:%Y-%m-%d %H:%M} {source}"
