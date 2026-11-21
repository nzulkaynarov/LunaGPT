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
from telegram_fakes import edited, pressed, prompt_cancel

from lunagpt.assistant import calendar_desk, school_desk
from lunagpt.assistant.calendar_desk import EventParts, plan
from lunagpt.assistant.gcal import CalendarError, GoogleCalendar
from lunagpt.assistant.school import TaskItem, TaskList
from lunagpt.assistant.telegram import DANGER
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
    data, prefix, args = pressed(preview.kwargs["reply_markup"], "✅ Добавить")
    confirm = _callback(data)
    await desk.handle(confirm, member, prefix, args)
    assert "✅ Добавила в календарь" in confirm.message.edit_text.call_args.args[0]
    assert google.created[0]["summary"] == "Родительское собрание"

    assert await desk.brief_lines(1, date(2026, 11, 11)) == ["📅 В календаре:", "• 15:00 — Стоматолог"]
    assert "сегодня 15:00–16:00: Стоматолог" in await desk.planner_text(1)

    _, markup = await desk.view(1)
    data, prefix, args = pressed(markup, "🔌 Отключить")
    unlink = _callback(data)
    await desk.handle(unlink, member, prefix, args)
    asked = unlink.message.edit_text.call_args
    assert asked.args[0] == calendar_desk.CONFIRM_UNLINK  # linking again takes the address again: it asks first
    assert "сегодня 15:00–16:00: Стоматолог" in await desk.planner_text(1)
    confirm_markup = asked.kwargs["reply_markup"]
    assert pressed(confirm_markup, "← Назад")[1] == "cal"
    assert [b.style for row in confirm_markup.inline_keyboard for b in row if b.text == "Да, отключить"] == [DANGER]
    data, prefix, args = pressed(confirm_markup, "Да, отключить")
    await desk.handle(_callback(data), member, prefix, args)
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
    data, prefix, args = pressed(telegram.send_message.call_args.kwargs["reply_markup"], "✅ Сохранить")
    save = _callback(data)
    await desk.handle(save, member, prefix, args)
    assert "В Google Calendar добавила: 1" in save.message.edit_text.call_args.args[0]
    assert google.created[1]["summary"] == "📝 Математика: № 125"

    google.items = [{"summary": "📝 Математика: № 125", "start": {"date": "2026-11-11"}, "end": {"date": "2026-11-12"}}]
    assert await calendar.brief_lines(1, date(2026, 11, 11)) == []  # the brief lists tasks itself

    async with db_sessions() as db:
        task = await db.scalar(select(AssistantTask).where(AssistantTask.subject == "Математика"))
    assert task is not None
    await desk.handle(_callback(f"tkd:{task.id}"), member, "tkd", [str(task.id)])
    assert google.deleted == [task.google_event_id]


@pytest.mark.parametrize(
    ("text", "adds"),
    [
        ("добавь в календарь встречу в пятницу в 15", True),
        ("запиши стоматолога в мой календарь на среду", True),
        ("посмотри в календарь, что завтра", False),
        ("что у меня в календаре на неделе?", False),
    ],
)
def test_only_adding_words_open_the_add_event_flow(text: str, adds: bool) -> None:
    assert calendar_desk.is_calendar_request(text) is adds


@pytest.mark.asyncio
async def test_nobody_links_a_calendar_someone_else_linked(db_sessions: async_sessionmaker[AsyncSession]) -> None:
    mama = await _member(db_sessions)
    kid = AssistantMember(telegram_id=2, name="Миша", role="kid")
    guest = AssistantMember(telegram_id=3, name="Гость", role="guest")
    async with db_sessions() as db:
        db.add_all([kid, guest])
        await db.commit()
    google = FakeGoogle()
    telegram = MagicMock(send_message=AsyncMock())
    desk = calendar_desk.CalendarDesk(telegram, db_sessions, _calendar(google), AsyncMock())
    await desk.link(mama, "mama@gmail.com")
    await desk.link(kid, "mama@gmail.com")
    asked = telegram.send_message.call_args
    assert asked.args[1] == f"{calendar_desk.TAKEN}\n{calendar_desk.LINK_PROMPT}"
    assert prompt_cancel(asked.kwargs["reply_markup"]) == calendar_desk.LINK_CANCEL
    await desk.link(guest, "guest@gmail.com")
    assert telegram.send_message.call_args.args[1] == calendar_desk.GUESTS_NO_CALENDAR
    async with db_sessions() as db:
        assert (await db.get(AssistantMember, 2)).google_calendar_id is None  # type: ignore[union-attr]


def _answer_to(prompt: str) -> MagicMock:
    answer = MagicMock(spec=Message)
    answer.reply_to_message = MagicMock(text=prompt, from_user=MagicMock(is_bot=True))
    return answer


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("address", "google", "problem"),
    [
        ("не адрес", FakeGoogle(), calendar_desk.NOT_AN_ADDRESS),
        ("papa@gmail.com", FakeGoogle(), calendar_desk.NOT_SHARED.format(email=SA_EMAIL)),
        ("mama@gmail.com", None, calendar_desk.UNAVAILABLE),
    ],
)
async def test_a_wrong_address_brings_a_fresh_prompt_that_still_works(
    db_sessions: async_sessionmaker[AsyncSession], address: str, google: FakeGoogle | None, problem: str
) -> None:
    member = await _member(db_sessions)
    telegram = MagicMock(send_message=AsyncMock())
    down = GoogleCalendar(_credentials(), 5, httpx.MockTransport(lambda request: httpx.Response(503)))
    desk = calendar_desk.CalendarDesk(telegram, db_sessions, _calendar(google) if google else down, AsyncMock())
    await desk.link(member, address)
    asked = telegram.send_message.call_args
    assert asked.args[1] == f"{problem}\n{calendar_desk.LINK_PROMPT}" and "ещё раз" not in problem
    assert prompt_cancel(asked.kwargs["reply_markup"]) == calendar_desk.LINK_CANCEL
    assert desk.is_link_reply(_answer_to(asked.args[1]))

    desk._calendar = _calendar(FakeGoogle())  # Google is back, or the address is right this time
    await desk.link(member, "mama@gmail.com")
    assert "📅 Календарь подключён." in [call.args[1] for call in telegram.send_message.call_args_list]


@pytest.mark.asyncio
async def test_the_address_prompt_can_be_cancelled(db_sessions: async_sessionmaker[AsyncSession]) -> None:
    member = await _member(db_sessions)
    telegram = MagicMock(send_message=AsyncMock())
    desk = calendar_desk.CalendarDesk(telegram, db_sessions, _calendar(FakeGoogle()), AsyncMock())
    await desk.handle(_callback("calc"), member, "calc", [])
    asked = telegram.send_message.call_args
    assert asked.args[1] == calendar_desk.LINK_PROMPT
    cancel = _callback(prompt_cancel(asked.kwargs["reply_markup"]))
    cancel.message.delete = AsyncMock()
    telegram.send_message.reset_mock()
    await desk.handle(cancel, member, calendar_desk.LINK_CANCEL, [])
    cancel.message.delete.assert_awaited_once()  # the calendar screen that asked stays above it
    cancel.message.edit_text.assert_not_awaited()
    telegram.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_every_preview_adds_exactly_the_event_it_shows(
    db_sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(calendar_desk, "local_now", lambda: NOW)
    member = await _member(db_sessions)
    async with db_sessions() as db:
        (await db.get(AssistantMember, 1)).google_calendar_id = "mama@gmail.com"  # type: ignore[union-attr]
        await db.commit()
    google = FakeGoogle()
    telegram = MagicMock(send_message=AsyncMock())
    extract = AsyncMock(
        side_effect=[
            EventParts(title="Стоматолог", day="thu", time="15:00"),
            EventParts(title="Собрание", day="fri"),
            EventParts(title="Кружок", day="sat"),
        ]
    )
    desk = calendar_desk.CalendarDesk(telegram, db_sessions, _calendar(google), extract)
    await desk.request_event(member, "добавь в календарь стоматолог в четверг в 15:00")
    old = telegram.send_message.call_args.kwargs["reply_markup"]
    await desk.request_event(member, "добавь в календарь собрание в пятницу")
    new = telegram.send_message.call_args.kwargs["reply_markup"]
    await desk.request_event(member, "добавь в календарь кружок в субботу")
    third = telegram.send_message.call_args.kwargs["reply_markup"]

    data, prefix, args = pressed(old, "✅ Добавить")  # the older preview, after a newer one came
    await desk.handle(_callback(data), member, prefix, args)
    assert [event["summary"] for event in google.created] == ["Стоматолог"]
    again = _callback(data)
    await desk.handle(again, member, prefix, args)
    assert edited(again.message) == calendar_desk.EXPIRED  # an event is added once
    data, prefix, args = pressed(new, "✅ Добавить")
    await desk.handle(_callback(data), member, prefix, args)
    assert [event["summary"] for event in google.created] == ["Стоматолог", "Собрание"]

    for markup in (third, old):  # a cancel says so, whether or not its preview is still waiting
        data, prefix, args = pressed(markup, "✖ Отмена")
        cancel = _callback(data)
        await desk.handle(cancel, member, prefix, args)
        shown = cancel.message.edit_text.call_args
        assert shown.args == (calendar_desk.CANCELLED,) and shown.kwargs["reply_markup"] is None
    assert len(google.created) == 2


@pytest.mark.asyncio
async def test_a_failed_add_keeps_the_preview_to_press_again(
    db_sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(calendar_desk, "local_now", lambda: NOW)
    member = await _member(db_sessions)
    async with db_sessions() as db:
        (await db.get(AssistantMember, 1)).google_calendar_id = "mama@gmail.com"  # type: ignore[union-attr]
        await db.commit()
    telegram = MagicMock(send_message=AsyncMock())
    extract = AsyncMock(return_value=EventParts(title="Стоматолог", day="thu", time="15:00"))
    down = GoogleCalendar(_credentials(), 5, httpx.MockTransport(lambda request: httpx.Response(503)))
    desk = calendar_desk.CalendarDesk(telegram, db_sessions, down, extract)
    await desk.request_event(member, "добавь в календарь стоматолог в четверг в 15:00")
    preview, markup = telegram.send_message.call_args.args[1], telegram.send_message.call_args.kwargs["reply_markup"]
    data, prefix, args = pressed(markup, "✅ Добавить")
    failed = _callback(data)
    await desk.handle(failed, member, prefix, args)
    shown = failed.message.edit_text.call_args
    assert shown.args[0] == f"{calendar_desk.UNAVAILABLE}\n\n{preview}"  # the event is still on screen
    assert pressed(shown.kwargs["reply_markup"], "✅ Добавить")[0] == data

    google = FakeGoogle()
    desk._calendar = _calendar(google)  # Google is back: the same button adds it, nothing to type again
    await desk.handle(_callback(data), member, prefix, args)
    assert [event["summary"] for event in google.created] == ["Стоматолог"]


@pytest.mark.asyncio
async def test_guests_are_offered_reminders_instead_of_the_calendar(
    db_sessions: async_sessionmaker[AsyncSession],
) -> None:
    guest = AssistantMember(telegram_id=3, name="Гость", role="guest")
    async with db_sessions() as db:
        db.add(guest)
        await db.commit()
    telegram = MagicMock(send_message=AsyncMock())
    extract = AsyncMock()
    desk = calendar_desk.CalendarDesk(telegram, db_sessions, _calendar(FakeGoogle()), extract)
    await desk.request_event(guest, "добавь в календарь стоматолог в четверг в 15:00")
    said = telegram.send_message.call_args.args[1]
    assert said == calendar_desk.GUESTS_NO_CALENDAR and "Мой день" not in said and "«напомни" in said
    extract.assert_not_awaited()

    old_button = _callback("calc")  # a screen sent before the button was hidden
    await desk.handle(old_button, guest, "calc", [])
    shown = old_button.message.edit_text.call_args
    assert shown.args == (calendar_desk.GUESTS_NO_CALENDAR,)
    assert pressed(shown.kwargs["reply_markup"], "← Мой день")[1] == "rm"
    telegram.send_message.assert_awaited_once()  # no address prompt was sent


# ---------- an event from the calendar screen ----------


async def _linked(db_sessions: async_sessionmaker[AsyncSession]) -> AssistantMember:
    member = await _member(db_sessions)
    async with db_sessions() as db:
        (await db.get(AssistantMember, 1)).google_calendar_id = "mama@gmail.com"  # type: ignore[union-attr]
        await db.commit()
    return member


def _labels(markup: Any) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


@pytest.mark.asyncio
async def test_only_a_linked_calendar_offers_a_new_event(db_sessions: async_sessionmaker[AsyncSession]) -> None:
    member = await _member(db_sessions)
    desk = calendar_desk.CalendarDesk(MagicMock(send_message=AsyncMock()), db_sessions, _calendar(FakeGoogle()))
    _, markup = await desk.view(1)
    assert calendar_desk.BTN_ADD_EVENT not in _labels(markup)  # not linked yet: there is nowhere to add
    await desk.link(member, "mama@gmail.com")
    text, markup = await desk.view(1)
    assert pressed(markup, calendar_desk.BTN_ADD_EVENT)[1] == calendar_desk.ADD_EVENT
    assert calendar_desk.BTN_ADD_EVENT in text

    guest = AssistantMember(telegram_id=3, name="Гость", role="guest")
    async with db_sessions() as db:
        db.add(guest)
        await db.commit()
    old_screen = _callback(calendar_desk.ADD_EVENT)
    await desk.handle(old_screen, guest, calendar_desk.ADD_EVENT, [])
    shown = old_screen.message.edit_text.call_args
    assert shown.args == (calendar_desk.GUESTS_NO_CALENDAR,)  # guests have no calendar, so no question either
    assert calendar_desk.BTN_ADD_EVENT not in _labels(shown.kwargs["reply_markup"])


@pytest.mark.asyncio
async def test_a_new_event_from_the_screen_asks_again_then_previews_and_leads_back(
    db_sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(calendar_desk, "local_now", lambda: NOW)
    member = await _linked(db_sessions)
    google = FakeGoogle()
    telegram = MagicMock(send_message=AsyncMock())
    extract = AsyncMock(
        side_effect=[
            EventParts(title="Стоматолог"),  # no day or time
            None,  # the model could not read it
            EventParts(title="Стоматолог", day="thu", time="15:00"),
        ]
    )
    desk = calendar_desk.CalendarDesk(telegram, db_sessions, _calendar(google), extract)
    press = _callback(calendar_desk.ADD_EVENT)
    press.message.delete = AsyncMock()
    await desk.handle(press, member, calendar_desk.ADD_EVENT, [])
    asked = telegram.send_message.call_args
    assert asked.args[1] == calendar_desk.EVENT_PROMPT and "«стоматолог в четверг в 15:00»" in asked.args[1]
    assert prompt_cancel(asked.kwargs["reply_markup"]) == calendar_desk.EVENT_CANCEL
    press.message.delete.assert_awaited_once()  # the week made way for the question
    assert desk.is_event_reply(_answer_to(asked.args[1]))

    await desk.request_event(member, "стоматолог", from_screen=True)
    again = telegram.send_message.call_args
    assert again.args[1] == f"{calendar_desk.EVENT_WHEN_MISSING}\n{calendar_desk.EVENT_PROMPT}"
    assert prompt_cancel(again.kwargs["reply_markup"]) == calendar_desk.EVENT_CANCEL
    assert desk.is_event_reply(_answer_to(again.args[1]))  # the fresh question still works
    await desk.request_event(member, "стоматолог в четверг", from_screen=True)
    assert telegram.send_message.call_args.args[1] == f"{calendar_desk.EVENT_TRY_AGAIN}\n{calendar_desk.EVENT_PROMPT}"

    await desk.request_event(member, "стоматолог в четверг в 15:00", from_screen=True)
    assert extract.call_args.args[0] == "добавь в календарь стоматолог в четверг в 15:00"  # read like the phrase
    preview = telegram.send_message.call_args
    assert preview.args[1] == "📅 Добавить в календарь?\n\nСтоматолог\nзавтра 15:00–16:00"
    data, prefix, args = pressed(preview.kwargs["reply_markup"], "✅ Добавить")
    confirm = _callback(data)
    await desk.handle(confirm, member, prefix, args)
    shown = confirm.message.edit_text.call_args
    assert shown.args[0].startswith("✅ Добавила в календарь: Стоматолог")
    assert pressed(shown.kwargs["reply_markup"], calendar_desk.BACK_TO_CALENDAR)[1] == "cal"
    assert [event["summary"] for event in google.created] == ["Стоматолог"]


@pytest.mark.asyncio
async def test_a_cancelled_event_from_the_screen_leads_back_and_a_phrase_preview_stays_as_it_was(
    db_sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(calendar_desk, "local_now", lambda: NOW)
    member = await _linked(db_sessions)
    telegram = MagicMock(send_message=AsyncMock())
    extract = AsyncMock(return_value=EventParts(title="Собрание", day="fri"))
    desk = calendar_desk.CalendarDesk(telegram, db_sessions, _calendar(FakeGoogle()), extract)
    await desk.request_event(member, "собрание в пятницу", from_screen=True)
    data, prefix, args = pressed(telegram.send_message.call_args.kwargs["reply_markup"], "✖ Отмена")
    cancel = _callback(data)
    await desk.handle(cancel, member, prefix, args)
    shown = cancel.message.edit_text.call_args
    assert shown.args == (calendar_desk.CANCELLED,)
    assert pressed(shown.kwargs["reply_markup"], calendar_desk.BACK_TO_CALENDAR)[1] == "cal"

    await desk.request_event(member, "добавь в календарь собрание в пятницу")
    assert extract.call_args.args[0] == "добавь в календарь собрание в пятницу"
    phrase_markup = telegram.send_message.call_args.kwargs["reply_markup"]
    assert [b.callback_data.count(":") for row in phrase_markup.inline_keyboard for b in row] == [1, 1]

    question = _callback(calendar_desk.EVENT_CANCEL)  # "✖ Отмена" under the question itself
    question.message.delete = AsyncMock()
    telegram.send_message.reset_mock()
    await desk.handle(question, member, calendar_desk.EVENT_CANCEL, [])
    question.message.delete.assert_awaited_once()
    week = telegram.send_message.call_args
    assert week.args[1].startswith("📅 Календарь mama@gmail.com на неделю")
    assert calendar_desk.BTN_ADD_EVENT in _labels(week.kwargs["reply_markup"])
