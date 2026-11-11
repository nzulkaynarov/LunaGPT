"""Google Calendar: the REST calls, linking, adding from a phrase, the brief and chat context."""

import json
from datetime import date, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lunagpt.assistant import calendar_desk, school_desk
from lunagpt.assistant.calendar_desk import EventParts, plan
from lunagpt.assistant.gcal import CalendarError, GoogleCalendar
from lunagpt.assistant.school import TaskItem, TaskList
from lunagpt.models import AssistantMember, AssistantTask

NOW = datetime(2026, 11, 11, 10, 30)  # a Wednesday
SA_EMAIL = "luna@family.iam.gserviceaccount.com"


def _credentials() -> MagicMock:
    return MagicMock(valid=True, token="tok", service_account_email=SA_EMAIL)


class FakeGoogle:
    """Answers like the Calendar API for one shared calendar and records what was sent."""

    def __init__(self, shared: str = "mama@gmail.com") -> None:
        self.shared = shared
        self.created: list[dict[str, Any]] = []
        self.deleted: list[str] = []
        self.items: list[dict[str, Any]] = [
            {
                "summary": "Стоматолог",
                "start": {"dateTime": "2026-11-11T10:00:00Z"},
                "end": {"dateTime": "2026-11-11T11:00:00Z"},
            },
            {"summary": "Отпуск", "start": {"date": "2026-11-12"}, "end": {"date": "2026-11-13"}},
            {"summary": "Отменено", "status": "cancelled", "start": {"date": "2026-11-12"}},
        ]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer tok"
        if f"/calendars/{self.shared.replace('@', '%40')}/" not in request.url.raw_path.decode():
            return httpx.Response(404)
        if request.method == "POST":
            self.created.append(json.loads(request.content))
            return httpx.Response(200, json={"id": f"e{len(self.created)}"})
        if request.method == "DELETE":
            self.deleted.append(request.url.path.rsplit("/", 1)[-1])
            return httpx.Response(204)
        return httpx.Response(200, json={"items": self.items})


def _calendar(google: FakeGoogle) -> GoogleCalendar:
    return GoogleCalendar(_credentials(), 5, httpx.MockTransport(google))


@pytest.mark.asyncio
async def test_events_are_read_in_local_time() -> None:
    events = await _calendar(FakeGoogle()).events("mama@gmail.com", NOW, NOW + timedelta(days=7))
    assert [(e.title, e.start, e.all_day) for e in events] == [
        ("Стоматолог", datetime(2026, 11, 11, 15, 0), False),
        ("Отпуск", datetime(2026, 11, 12, 0, 0), True),
    ]


@pytest.mark.asyncio
async def test_unshared_calendar_and_outage_are_told_apart() -> None:
    with pytest.raises(CalendarError) as refused:
        await _calendar(FakeGoogle()).check("papa@gmail.com")
    assert refused.value.kind == "not_shared"
    down = GoogleCalendar(_credentials(), 5, httpx.MockTransport(lambda request: httpx.Response(503)))
    with pytest.raises(CalendarError) as failed:
        await down.check("mama@gmail.com")
    assert failed.value.kind == "unavailable"


@pytest.mark.asyncio
async def test_event_is_sent_with_the_family_offset() -> None:
    google = FakeGoogle()
    calendar = _calendar(google)
    await calendar.add(
        "mama@gmail.com", "Стоматолог", datetime(2026, 11, 12, 15, 0), datetime(2026, 11, 12, 16, 0), False
    )
    await calendar.add("mama@gmail.com", "Отпуск", datetime(2026, 12, 1), datetime(2026, 12, 2), True)
    assert google.created[0]["start"] == {"dateTime": "2026-11-12T10:00:00+00:00"}
    assert google.created[1]["start"] == {"date": "2026-12-01"} and google.created[1]["end"] == {"date": "2026-12-02"}


def test_a_broken_key_is_refused() -> None:
    with pytest.raises((ValueError, KeyError)):
        GoogleCalendar.from_json('{"type": "service_account"}', 5)


@pytest.mark.parametrize(
    ("values", "start", "end", "all_day"),
    [
        ({"day": "thu", "time": "15:00"}, datetime(2026, 11, 12, 15, 0), datetime(2026, 11, 12, 16, 0), False),
        (
            {"day": "tomorrow", "time": "18:30", "duration_minutes": 90},
            datetime(2026, 11, 12, 18, 30),
            datetime(2026, 11, 12, 20, 0),
            False,
        ),
        ({"day": "2026-11-25"}, datetime(2026, 11, 25), datetime(2026, 11, 26), True),
        ({"in_days": 7, "time": "11:00"}, datetime(2026, 11, 18, 11, 0), datetime(2026, 11, 18, 12, 0), False),
    ],
)
def test_phrase_parts_become_an_event(values: dict[str, Any], start: datetime, end: datetime, all_day: bool) -> None:
    planned = plan(EventParts(title="Дело", **values), NOW)
    assert planned is not None and (planned.start, planned.end, planned.all_day) == (start, end, all_day)


def test_no_day_or_time_is_not_an_event() -> None:
    assert plan(EventParts(title="Дело"), NOW) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("добавь в календарь стоматолог в четверг", True),
        ("запиши встречу с Акмалем на пятницу", True),
        ("что у меня в календаре на завтра?", False),
        ("напомни завтра купить хлеб", False),
        ("запиши задачу оплатить интернет", False),
    ],
)
def test_calendar_requests_are_recognised(text: str, expected: bool) -> None:
    assert calendar_desk.is_calendar_request(text) is expected


# ---------- the desk against a real database ----------


async def _member(db_sessions: async_sessionmaker[AsyncSession]) -> AssistantMember:
    member = AssistantMember(telegram_id=1, name="Камила", role="adult")
    async with db_sessions() as db:
        db.add(member)
        await db.commit()
    return member


def _callback(data: str) -> MagicMock:
    callback = MagicMock(data=data, answer=AsyncMock())
    callback.message = MagicMock(spec=Message, edit_text=AsyncMock())
    return callback


@pytest.mark.asyncio
async def test_link_add_brief_and_chat(
    db_sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(calendar_desk, "local_now", lambda: NOW)
    member = await _member(db_sessions)
    google = FakeGoogle()
    telegram = MagicMock(send_message=AsyncMock())
    extract = AsyncMock(return_value=EventParts(title="Родительское собрание", day="tomorrow", time="18:30"))
    desk = calendar_desk.CalendarDesk(telegram, db_sessions, _calendar(google), extract)

    text, _ = await desk.view(1)
    assert SA_EMAIL in text and "Вносить изменения в мероприятия" in text
    await desk.request_event(member, "добавь в календарь собрание")
    assert telegram.send_message.call_args.args[1] == calendar_desk.NOT_LINKED

    await desk.link(member, "papa@gmail.com")
    assert SA_EMAIL in telegram.send_message.call_args.args[1]  # not shared: says whom to share with
    await desk.link(member, "Mama@Gmail.com")
    text, _ = await desk.view(1)
    assert "• сегодня 15:00–16:00 — Стоматолог" in text and "• завтра, весь день — Отпуск" in text

    await desk.request_event(member, "добавь в календарь родительское собрание завтра в 18:30")
    preview = telegram.send_message.call_args
    assert "Родительское собрание\nзавтра 18:30–19:30" in preview.args[1]
    confirm = _callback("caly")
    await desk.handle(confirm, member, "caly", [])
    assert "✅ Добавила в календарь" in confirm.message.edit_text.call_args.args[0]
    assert google.created[0]["summary"] == "Родительское собрание"

    assert await desk.brief_lines(1, date(2026, 11, 11)) == ["📅 В календаре:", "• 15:00 — Стоматолог"]
    assert "сегодня 15:00–16:00: Стоматолог" in await desk.planner_text(1)

    await desk.handle(_callback("calx"), member, "calx", [])
    assert await desk.planner_text(1) == ""


@pytest.mark.asyncio
async def test_without_a_key_calendar_is_off(db_sessions: async_sessionmaker[AsyncSession]) -> None:
    member = await _member(db_sessions)
    telegram = MagicMock(send_message=AsyncMock())
    desk = calendar_desk.CalendarDesk(telegram, db_sessions, None)
    text, _ = await desk.view(1)
    assert calendar_desk.NOT_CONFIGURED in text
    await desk.request_event(member, "добавь в календарь дело завтра")
    assert telegram.send_message.call_args.args[1] == calendar_desk.NOT_CONFIGURED
    assert await desk.brief_lines(1, date(2026, 11, 11)) == [] and await desk.planner_text(1) == ""


@pytest.mark.asyncio
async def test_tasks_with_a_deadline_follow_into_the_calendar(
    db_sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(calendar_desk, "local_now", lambda: NOW)
    monkeypatch.setattr(school_desk, "local_now", lambda: NOW)
    member = await _member(db_sessions)
    async with db_sessions() as db:
        db.add(AssistantTask(member_id=1, subject="История", text="параграф 12", due_on=date(2026, 11, 17)))
        db.add(AssistantTask(member_id=1, subject="", text="доклад"))  # no deadline: stays in Luna only
        await db.commit()
    google = FakeGoogle()
    telegram = MagicMock(send_message=AsyncMock())
    calendar = calendar_desk.CalendarDesk(telegram, db_sessions, _calendar(google))
    await calendar.link(member, "mama@gmail.com")
    assert "Перенесла туда заданий со сроком: 1" in telegram.send_message.call_args_list[0].args[1]
    assert google.created[0]["summary"] == "📝 История: параграф 12"
    assert google.created[0]["start"] == {"date": "2026-11-17"}

    found = TaskList(items=[TaskItem(subject="Математика", task="№ 125", due="fri")])
    desk = school_desk.SchoolDesk(telegram, db_sessions, read_tasks=AsyncMock(return_value=found), mirror=calendar)
    await desk.read_tasks(member, "задали")
    save = _callback("tky")
    await desk.handle(save, member, "tky", [])
    assert "В Google Calendar добавила: 1" in save.message.edit_text.call_args.args[0]
    assert google.created[1]["summary"] == "📝 Математика: № 125"

    google.items = [{"summary": "📝 Математика: № 125", "start": {"date": "2026-11-11"}, "end": {"date": "2026-11-12"}}]
    assert await calendar.brief_lines(1, date(2026, 11, 11)) == []  # the brief lists tasks itself

    async with db_sessions() as db:
        task = await db.scalar(select(AssistantTask).where(AssistantTask.subject == "Математика"))
    assert task is not None
    await desk.handle(_callback(f"tkd:{task.id}"), member, "tkd", [str(task.id)])
    assert google.deleted == [task.google_event_id]
