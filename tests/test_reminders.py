"""Reminders and the morning brief: phrase to moment, storage, ringing, snoozing and the brief text."""

from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telegram_fakes import fake_bot, last_sent, prompt_cancel

from lunagpt.assistant import brief, reminder_desk, reminders
from lunagpt.assistant.reminders import ReminderParts
from lunagpt.assistant.telegram import Rich, to_plain
from lunagpt.models import AssistantMember, AssistantReminder

NOW = datetime(2026, 11, 11, 10, 30)  # a Wednesday


def _parts(**values: Any) -> ReminderParts:
    return ReminderParts.model_validate({"task": "дело", **values})


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ({"day": "tomorrow", "time": "09:00"}, datetime(2026, 11, 12, 9, 0)),
        ({"in_minutes": 20}, datetime(2026, 11, 11, 10, 50)),
        ({"time": "09:00"}, datetime(2026, 11, 12, 9, 0)),  # already past today
        ({"time": "19:00"}, datetime(2026, 11, 11, 19, 0)),
        ({"day": "fri", "time": "18:00"}, datetime(2026, 11, 13, 18, 0)),
        ({"day": "wed", "time": "09:00"}, datetime(2026, 11, 18, 9, 0)),  # this Wednesday's 9:00 has passed
        ({"day": "tomorrow"}, datetime(2026, 11, 12, 9, 0)),  # no time: 9 in the morning
        ({"day": "2026-11-25", "time": "12:00"}, datetime(2026, 11, 25, 12, 0)),
        ({"time": "08:00", "repeat": "daily"}, datetime(2026, 11, 12, 8, 0)),
        ({"day": "daily", "time": "08:00", "repeat": "daily"}, datetime(2026, 11, 12, 8, 0)),
    ],
)
def test_phrase_parts_become_a_moment(values: dict[str, Any], expected: datetime) -> None:
    assert reminders.resolve(_parts(**values), NOW) == expected


def test_weekdays_skip_the_weekend() -> None:
    friday_evening = datetime(2026, 11, 13, 20, 0)
    assert reminders.resolve(_parts(time="07:30", repeat="weekdays"), friday_evening) == datetime(2026, 11, 16, 7, 30)


@pytest.mark.parametrize("values", [{}, {"day": "2028-01-01", "time": "10:00"}])
def test_no_time_or_too_far_is_refused(values: dict[str, Any]) -> None:
    assert reminders.resolve(_parts(**values), NOW) is None


def test_malformed_parts_are_rejected() -> None:
    with pytest.raises(ValueError):
        _parts(day="someday")
    with pytest.raises(ValueError):
        _parts(time="25:00")


@pytest.mark.parametrize(
    ("repeat", "due", "now", "expected"),
    [
        ("daily", datetime(2026, 11, 11, 8, 0), NOW, datetime(2026, 11, 12, 8, 0)),
        ("weekly", datetime(2026, 11, 11, 8, 0), NOW, datetime(2026, 11, 18, 8, 0)),
        ("weekdays", datetime(2026, 11, 13, 7, 30), datetime(2026, 11, 13, 7, 31), datetime(2026, 11, 16, 7, 30)),
        ("daily", datetime(2026, 11, 1, 8, 0), NOW, datetime(2026, 11, 12, 8, 0)),  # after days offline
        ("none", datetime(2026, 11, 11, 8, 0), NOW, None),
    ],
)
def test_repeats_move_forward(repeat: str, due: datetime, now: datetime, expected: datetime | None) -> None:
    assert reminders.next_time(due, repeat, now) == expected


def test_when_text_is_human() -> None:
    assert reminders.when_text(datetime(2026, 11, 11, 18, 0), NOW) == "сегодня в 18:00"
    assert reminders.when_text(datetime(2026, 11, 12, 9, 0), NOW) == "завтра в 09:00"
    assert reminders.when_text(datetime(2026, 11, 13, 9, 0), NOW) == "послезавтра в 09:00"
    assert reminders.when_text(datetime(2026, 11, 16, 9, 0), NOW) == "в понедельник в 09:00"
    assert reminders.when_text(datetime(2026, 11, 12, 8, 0), NOW, "weekdays") == "по будням в 08:00"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("напомни завтра купить хлеб", True),
        ("Напоминай каждый день пить воду", True),
        ("Remind me at 6 to call", True),
        ("что такое напоминание", False),
        ("я забыл, напомни потом", False),
    ],
)
def test_reminder_requests_are_recognised(text: str, expected: bool) -> None:
    assert reminders.is_reminder_request(text) is expected


@pytest.mark.parametrize(
    ("brief_time", "sent_on", "now", "expected"),
    [
        ("07:30", None, datetime(2026, 11, 11, 7, 30), True),
        ("07:30", None, datetime(2026, 11, 11, 7, 29), False),
        ("07:30", date(2026, 11, 11), datetime(2026, 11, 11, 8, 0), False),
        ("07:30", date(2026, 11, 10), datetime(2026, 11, 11, 11, 0), False),  # stale after three hours
        (None, None, datetime(2026, 11, 11, 8, 0), False),
    ],
)
def test_brief_is_due_once_a_morning(
    brief_time: str | None, sent_on: date | None, now: datetime, expected: bool
) -> None:
    assert brief.is_due(brief_time, sent_on, now) is expected


def test_brief_text() -> None:
    weather = brief.weather_line("Ташкент", brief.Forecast(low=2.0, high=9.4, rain_chance=80, code=61))
    assert weather == "🌡 Ташкент: от 2° до 9°, небольшой дождь 🌦. Осадки 80%, возьми зонт ☂️."
    screen = brief.compose("Нурбек", date(2026, 11, 11), weather, [(datetime(2026, 11, 11, 9, 0), "позвонить")])
    text = to_plain(screen)
    assert isinstance(screen, Rich) and "<table" in screen
    assert text.startswith("☀️ Доброе утро, Нурбек!\nСегодня среда, 11 ноября.")
    assert "🔔 Сегодня\n09:00 · позвонить" in text
    assert "Напоминаний на сегодня нет" in brief.compose("Нурбек", date(2026, 11, 11), None, [])
    assert "&lt;b&gt;" in brief.compose("<b>", date(2026, 11, 11), None, [])  # names are escaped


@pytest.mark.asyncio
async def test_weather_and_city_come_from_open_meteo() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host.startswith("geocoding"):
            assert request.url.params["name"] == "Ташкент"
            return httpx.Response(200, json={"results": [{"name": "Ташкент", "latitude": 41.3, "longitude": 69.2}]})
        daily = {
            "temperature_2m_min": [2.0],
            "temperature_2m_max": [9.1],
            "precipitation_probability_max": [None],
            "weather_code": [3],
        }
        return httpx.Response(200, json={"daily": daily})

    transport = httpx.MockTransport(handler)
    assert await brief.find_city("Ташкент", transport) == brief.Place("Ташкент", 41.3, 69.2)
    assert await brief.forecast(41.3, 69.2, transport) == brief.Forecast(2.0, 9.1, None, 3)
    failing = httpx.MockTransport(lambda request: httpx.Response(500))
    assert await brief.find_city("Ташкент", failing) is None
    assert await brief.forecast(1.0, 1.0, failing) is None


# ---------- the desk against a real database ----------


def _telegram() -> MagicMock:
    return fake_bot()


def _last(telegram: MagicMock) -> str:
    return last_sent(telegram)[0]


async def _member(db_sessions: async_sessionmaker[AsyncSession], telegram_id: int = 1) -> AssistantMember:
    member = AssistantMember(telegram_id=telegram_id, name="Тест", role="adult")
    async with db_sessions() as db:
        db.add(member)
        await db.commit()
    return member


async def _guest(db_sessions: async_sessionmaker[AsyncSession]) -> AssistantMember:
    guest = AssistantMember(telegram_id=3, name="Гость", role="guest")
    async with db_sessions() as db:
        db.add(guest)
        await db.commit()
    return guest


def _callback(data: str, user_id: int = 1) -> MagicMock:
    callback = MagicMock(data=data, answer=AsyncMock())
    callback.from_user.id = user_id
    callback.message = MagicMock(spec=Message, text="🔔 дело", edit_text=AsyncMock())
    return callback


@pytest.mark.asyncio
async def test_reminder_is_saved_confirmed_and_cancelled(
    db_sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reminder_desk, "local_now", lambda: NOW)
    member = await _member(db_sessions)
    telegram = _telegram()
    extract = AsyncMock(return_value=_parts(task="позвонить бабушке", day="tomorrow", time="09:00"))
    desk = reminder_desk.ReminderDesk(telegram, db_sessions, extract)
    await desk.create(member, "напомни завтра в 9 позвонить бабушке")
    assert _last(telegram) == "🔔 Напомню завтра в 09:00: позвонить бабушке"
    async with db_sessions() as db:
        row = await db.scalar(select(AssistantReminder))
    assert row is not None and reminder_desk.to_local(row.due_at) == datetime(2026, 11, 12, 9, 0)

    text, _ = await desk.view(member)
    assert "1 · завтра в 09:00 · позвонить бабушке" in to_plain(text)
    stranger = _callback(f"rmd:{row.id}", user_id=2)
    await desk.handle(stranger, await _member(db_sessions, 2), "rmd", [str(row.id)])
    async with db_sessions() as db:
        assert await db.get(AssistantReminder, row.id) is not None  # nobody deletes another person's reminder
    await desk.handle(_callback(f"rmd:{row.id}"), member, "rmd", [str(row.id)])
    async with db_sessions() as db:
        assert await db.get(AssistantReminder, row.id) is None


@pytest.mark.asyncio
async def test_unclear_phrase_saves_nothing(db_sessions: async_sessionmaker[AsyncSession]) -> None:
    member = await _member(db_sessions)
    telegram = _telegram()
    desk = reminder_desk.ReminderDesk(telegram, db_sessions, AsyncMock(return_value=_parts()))
    await desk.create(member, "напомни про дело")
    assert _last(telegram) == reminder_desk.NOT_UNDERSTOOD
    desk = reminder_desk.ReminderDesk(telegram, db_sessions, AsyncMock(return_value=None))
    await desk.create(member, "напомни")
    assert _last(telegram) == reminder_desk.NOT_PARSED
    async with db_sessions() as db:
        assert await db.scalar(select(AssistantReminder)) is None


@pytest.mark.asyncio
async def test_due_reminders_ring_once_and_repeats_move_on(db_sessions: async_sessionmaker[AsyncSession]) -> None:
    await _member(db_sessions)
    past = datetime.now(UTC) - timedelta(minutes=1)
    async with db_sessions() as db:
        db.add(AssistantReminder(member_id=1, text="разово", due_at=past))
        db.add(AssistantReminder(member_id=1, text="каждый день", due_at=past, repeat="daily"))
        db.add(AssistantReminder(member_id=1, text="потом", due_at=past + timedelta(hours=2)))
        await db.commit()
    telegram = _telegram()
    desk = reminder_desk.ReminderDesk(telegram, db_sessions)
    assert await desk.ring_due() == 2
    assert await desk.ring_due() == 0
    async with db_sessions() as db:
        rows = {r.text: r for r in await db.scalars(select(AssistantReminder))}
    assert rows["разово"].done_at is not None
    assert rows["каждый день"].done_at is None and rows["каждый день"].due_at == past + timedelta(days=1)
    keyboard = telegram.send_message.call_args.kwargs["reply_markup"].inline_keyboard[0]
    assert [b.callback_data.split(":")[0] for b in keyboard] == ["rmz", "rmz", "rmok"]


@pytest.mark.asyncio
async def test_late_ring_says_it_is_late(db_sessions: async_sessionmaker[AsyncSession]) -> None:
    await _member(db_sessions)
    async with db_sessions() as db:
        db.add(AssistantReminder(member_id=1, text="дело", due_at=datetime.now(UTC) - timedelta(hours=3)))
        await db.commit()
    telegram = _telegram()
    await reminder_desk.ReminderDesk(telegram, db_sessions).ring_due()
    assert "я была выключена" in _last(telegram)


@pytest.mark.asyncio
async def test_snooze_rings_again_later(db_sessions: async_sessionmaker[AsyncSession]) -> None:
    member = await _member(db_sessions)
    async with db_sessions() as db:
        row = AssistantReminder(member_id=1, text="дело", due_at=datetime.now(UTC), done_at=datetime.now(UTC))
        db.add(row)
        await db.commit()
    desk = reminder_desk.ReminderDesk(_telegram(), db_sessions)
    callback = _callback(f"rmz:{row.id}:10")
    await desk.handle(callback, member, "rmz", [str(row.id), "10"])
    assert "Напомню ещё раз" in callback.message.edit_text.call_args.args[0]
    async with db_sessions() as db:
        again = await db.scalar(select(AssistantReminder).where(AssistantReminder.done_at.is_(None)))
    assert again is not None and again.due_at > datetime.now(UTC) + timedelta(minutes=9)


@pytest.mark.asyncio
async def test_snooze_of_a_deleted_reminder_says_so_and_drops_the_buttons(
    db_sessions: async_sessionmaker[AsyncSession],
) -> None:
    member = await _member(db_sessions)
    desk = reminder_desk.ReminderDesk(_telegram(), db_sessions)
    callback = _callback("rmz:404:10")
    await desk.handle(callback, member, "rmz", ["404", "10"])
    shown = callback.message.edit_text.call_args
    assert shown.args == (f"🔔 дело\n\n{reminder_desk.SNOOZE_GONE}",) and shown.kwargs["reply_markup"] is None
    async with db_sessions() as db:
        assert await db.scalar(select(AssistantReminder)) is None  # nothing rings later


@pytest.mark.asyncio
async def test_done_closes_a_ring_and_keeps_a_repeating_series(db_sessions: async_sessionmaker[AsyncSession]) -> None:
    member = await _member(db_sessions)
    now = datetime.now(UTC)
    async with db_sessions() as db:
        series = AssistantReminder(member_id=1, text="таблетка", due_at=now + timedelta(days=1), repeat="daily")
        one_off = AssistantReminder(member_id=1, text="дело", due_at=now)  # rang, but not closed in time
        db.add_all([series, one_off])
        await db.commit()
    finish = AsyncMock(return_value=True)
    desk = reminder_desk.ReminderDesk(_telegram(), db_sessions, finish_task=finish)
    for row in (series, one_off):
        callback = _callback(f"rmok:{row.id}")
        await desk.handle(callback, member, "rmok", [str(row.id)])
        shown = callback.message.edit_text.call_args
        assert shown.args == ("✅ 🔔 дело",) and shown.kwargs["reply_markup"] is None
    finish.assert_not_awaited()  # neither is homework
    async with db_sessions() as db:
        assert (await db.get(AssistantReminder, series.id)).done_at is None  # type: ignore[union-attr]
        assert (await db.get(AssistantReminder, one_off.id)).done_at is not None  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_brief_settings_and_morning_send(
    db_sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    member = await _member(db_sessions)
    telegram = _telegram()
    geocode = AsyncMock(return_value=brief.Place("Ташкент", 41.3, 69.2))
    weather = AsyncMock(return_value=brief.Forecast(1.0, 8.0, 10, 0))
    desk = reminder_desk.ReminderDesk(telegram, db_sessions, AsyncMock(), geocode, weather)
    callback = _callback("rbt:0730")
    await desk.handle(callback, member, "rbt", ["0730"])
    assert "✅ 07:30" in str(callback.message.edit_text.call_args.kwargs["reply_markup"])
    await desk.set_city(member, "ташкент")

    morning = datetime(2026, 11, 11, 7, 31)
    monkeypatch.setattr(reminder_desk, "local_now", lambda: morning)
    async with db_sessions() as db:
        db.add(
            AssistantReminder(
                member_id=1, text="собрание", due_at=reminder_desk.to_utc(morning.replace(hour=18, minute=0))
            )
        )
        db.add(AssistantReminder(member_id=1, text="завтра", due_at=reminder_desk.to_utc(morning + timedelta(days=1))))
        await db.commit()
    assert await desk.send_briefs() == 1
    sent = _last(telegram)
    assert "Ташкент: от 1° до 8°, ясно" in sent and "18:00 · собрание" in sent and "завтра" not in sent
    assert await desk.send_briefs() == 0  # once a morning


@pytest.mark.asyncio
async def test_my_day_shows_the_calendar_only_once_google_is_connected(
    db_sessions: async_sessionmaker[AsyncSession],
) -> None:
    member = await _member(db_sessions)
    hidden = reminder_desk.ReminderDesk(_telegram(), db_sessions)
    shown = reminder_desk.ReminderDesk(_telegram(), db_sessions, calendar=True)
    text, markup = await hidden.view(member)
    assert to_plain(text).startswith(reminder_desk.DAY_TITLE)
    assert "cal" not in [b.callback_data for row in markup.inline_keyboard for b in row]
    _, markup = await shown.view(member)
    assert "cal" in [b.callback_data for row in markup.inline_keyboard for b in row]
    _, markup = await shown.view(await _guest(db_sessions))
    assert "cal" not in [b.callback_data for row in markup.inline_keyboard for b in row]  # guests cannot link one


@pytest.mark.parametrize("value", ["abcd", "2560", "9", "07:30", ""])
def test_only_real_brief_times_are_accepted(value: str) -> None:
    from lunagpt.assistant import reminder_desk

    assert not reminder_desk.BRIEF_TIME.fullmatch(value)
    assert reminder_desk.BRIEF_TIME.fullmatch("0730") and reminder_desk.BRIEF_TIME.fullmatch("2359")


def test_button_colours_mean_the_same_everywhere() -> None:
    from lunagpt.assistant import school_desk
    from lunagpt.assistant.telegram import SUCCESS

    def styles(markup: object) -> dict[str, str | None]:
        return {b.text: b.style for row in markup.inline_keyboard for b in row}  # type: ignore[attr-defined]

    assert styles(reminder_desk.ring_keyboard(1))["✅ Готово"] == SUCCESS
    assert styles(reminder_desk.ring_keyboard(1))["⏰ +10 мин"] is None
    assert styles(school_desk._confirm("sk", "token"))["✅ Сохранить"] == SUCCESS


@pytest.mark.asyncio
async def test_city_prompt_can_be_cancelled_and_an_unknown_city_asks_again(
    db_sessions: async_sessionmaker[AsyncSession],
) -> None:
    member = await _member(db_sessions)
    telegram = _telegram()
    geocode = AsyncMock(side_effect=[None, brief.Place("Ташкент", 41.3, 69.2)])
    desk = reminder_desk.ReminderDesk(telegram, db_sessions, AsyncMock(), geocode)
    await desk.handle(_callback("rbc"), member, "rbc", [])
    text, markup = last_sent(telegram)
    assert text == reminder_desk.CITY_PROMPT and prompt_cancel(markup) == reminder_desk.CITY_CANCEL

    cancel = _callback(reminder_desk.CITY_CANCEL)
    cancel.message.delete = AsyncMock()
    telegram.reset_mock()
    await desk.handle(cancel, member, reminder_desk.CITY_CANCEL, [])
    cancel.message.delete.assert_awaited_once()  # the brief screen that asked stays above it
    cancel.message.edit_text.assert_not_awaited()
    assert not telegram.mock_calls

    await desk.set_city(member, "Нигдеград")
    fresh, markup = last_sent(telegram)
    assert fresh == f"{reminder_desk.CITY_NOT_FOUND}\n{reminder_desk.CITY_PROMPT}"
    assert prompt_cancel(markup) == reminder_desk.CITY_CANCEL
    answer = MagicMock(spec=Message)
    answer.reply_to_message = MagicMock(text=fresh, from_user=MagicMock(is_bot=True))
    assert desk.is_city_reply(answer)
    await desk.set_city(member, "ташкент")
    assert "🏙 Город для погоды: Ташкент" in [str(call.args[1]) for call in telegram.send_message.call_args_list]
