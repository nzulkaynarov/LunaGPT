"""The single way in and the adults' decisions, against a real database."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from aiogram.types import Message, ReplyKeyboardRemove
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lunagpt.assistant import access, people
from lunagpt.assistant.people import PeopleDesk
from lunagpt.assistant.pi_client import PiConflictError, PiUnavailableError
from lunagpt.models import AssistantApplication, AssistantMember, AssistantMemory

OWNER, ADULT, NEW = 1, 2, 500
PI_PEOPLE = [
    {"id": 3, "name": "Марат", "role": "adult", "telegram_id": None},
    {"id": 4, "name": "Миша", "role": "kid", "telegram_id": None},
    {"id": 5, "name": "Занят", "role": "adult", "telegram_id": 999},
    {"id": 6, "name": "Сосед", "role": "guest", "telegram_id": None},
]


@pytest_asyncio.fixture
async def sessions(db_sessions: async_sessionmaker[AsyncSession]) -> async_sessionmaker[AsyncSession]:
    async with db_sessions() as db:
        db.add_all(
            [
                AssistantMember(telegram_id=OWNER, name="Глава", role="owner", requests_today=0),
                AssistantMember(telegram_id=ADULT, name="Аня", role="adult", requests_today=0),
            ]
        )
        await db.commit()
    return db_sessions


@pytest.fixture
def desk(sessions: Any) -> PeopleDesk:
    telegram = MagicMock(send_message=AsyncMock(side_effect=lambda *a, **k: MagicMock(message_id=a[0] * 10)))
    telegram.edit_message_text = AsyncMock()
    pi = MagicMock(people=AsyncMock(return_value=PI_PEOPLE), link_telegram=AsyncMock())
    return PeopleDesk(telegram, sessions, pi, "lunabot", AsyncMock())


def _message(user_id: int = NEW, text: str = "/start", contact_user: int | None = None) -> MagicMock:
    message = MagicMock(spec=Message, text=text)
    message.from_user = MagicMock(id=user_id, full_name="Новый", username="newbie")
    message.contact = (
        None if contact_user is None else MagicMock(user_id=contact_user, phone_number="+998901", first_name="Н")
    )
    return message


def _press(data: str) -> MagicMock:
    callback = MagicMock(data=data)
    callback.message = MagicMock(spec=Message, edit_text=AsyncMock())
    return callback


def _actor(role: str = "owner", telegram_id: int = OWNER) -> AssistantMember:
    return AssistantMember(telegram_id=telegram_id, name="Глава" if role == "owner" else "Аня", role=role)


async def _press_as(desk: PeopleDesk, data: str, actor: AssistantMember | None = None) -> MagicMock:
    callback = _press(data)
    prefix, *args = data.split(":")
    await desk.handle(callback, actor or _actor(), prefix, args)
    return callback


def _shown(callback: MagicMock) -> tuple[str, list[str]]:
    call = callback.message.edit_text.await_args
    markup = call.kwargs.get("reply_markup", None)
    rows = markup.inline_keyboard if markup else []
    return call.args[0], [button.callback_data for row in rows for button in row]


async def _apply(desk: PeopleDesk, user_id: int = NEW) -> None:
    await desk.on_stranger(_message(user_id, "", contact_user=user_id))


async def _application(sessions: Any, user_id: int = NEW) -> AssistantApplication | None:
    async with sessions() as db:
        return await access.get_application(db, user_id)


async def _member(sessions: Any, user_id: int = NEW) -> AssistantMember | None:
    async with sessions() as db:
        return await access.get_member(db, user_id)


def _sent_to(desk: Any) -> list[int]:
    return [call.args[0] for call in desk._bot.send_message.await_args_list]


@pytest.mark.asyncio
async def test_start_asks_for_contact(desk: Any) -> None:
    await desk.on_stranger(_message())
    assert desk._bot.send_message.await_args.args[1] == people.ASK_CONTACT
    keyboard = desk._bot.send_message.await_args.kwargs["reply_markup"]
    assert keyboard.keyboard[0][0].request_contact is True


@pytest.mark.asyncio
async def test_someone_elses_contact_is_refused(desk: Any, sessions: Any) -> None:
    await desk.on_stranger(_message(NEW, "", contact_user=777))
    assert desk._bot.send_message.await_args.args[1] == people.NOT_YOUR_CONTACT
    assert await _application(sessions) is None


@pytest.mark.asyncio
async def test_contact_creates_one_application_and_tells_every_adult(desk: Any, sessions: Any) -> None:
    await _apply(desk)
    application = await _application(sessions)
    assert application is not None and application.status == "pending" and application.phone == "+998901"
    assert set(_sent_to(desk)) == {NEW, OWNER, ADULT}
    text = next(c.args[1] for c in desk._bot.send_message.await_args_list if c.args[0] == OWNER)
    assert "Новый" in text and "@newbie" in text and "+998901" in text
    assert isinstance(desk._bot.send_message.await_args_list[0].kwargs["reply_markup"], ReplyKeyboardRemove)


@pytest.mark.asyncio
async def test_waiting_person_cannot_notify_again(desk: Any) -> None:
    await _apply(desk)
    desk._bot.send_message.reset_mock()
    await _apply(desk)
    assert _sent_to(desk) == [NEW]
    assert desk._bot.send_message.await_args.args[1] == people.STILL_WAITING


@pytest.mark.asyncio
async def test_rejected_person_notifies_nobody(desk: Any) -> None:
    await _apply(desk)
    await _press_as(desk, f"apr:{NEW}")
    desk._bot.send_message.reset_mock()
    desk._replied.clear()
    await _apply(desk)
    assert _sent_to(desk) == [NEW]
    assert desk._bot.send_message.await_args.args[1] == people.CLOSED


@pytest.mark.asyncio
async def test_card_offers_only_free_family_people(desk: Any, sessions: Any) -> None:
    await _apply(desk)
    application = await _application(sessions)
    text, markup = await desk._application_view(application)
    data = [button.callback_data for row in markup.inline_keyboard for button in row]
    assert f"apf:{NEW}:3" in data and f"apf:{NEW}:4" in data
    assert f"apf:{NEW}:5" not in data and f"apf:{NEW}:6" not in data
    assert "Кто это?" in text


@pytest.mark.asyncio
async def test_person_already_linked_in_luna_is_not_offered(desk: Any, sessions: Any) -> None:
    async with sessions() as db:
        db.add(AssistantMember(telegram_id=9, name="Марат", role="adult", requests_today=0, homesec_person_id=3))
        await db.commit()
    assert [p["id"] for p in await desk._free_people(NEW)] == [4]


@pytest.mark.asyncio
async def test_family_choice_takes_role_from_pi_and_erases_phone(desk: Any, sessions: Any) -> None:
    await _apply(desk)
    callback = await _press_as(desk, f"apf:{NEW}:4", _actor("adult", ADULT))
    member = await _member(sessions)
    assert member is not None and (member.role, member.name, member.homesec_person_id) == ("kid", "Миша", 4)
    assert await _application(sessions) is None
    desk._pi.link_telegram.assert_awaited_once_with(4, NEW)
    desk._welcome.assert_awaited_once_with(NEW, "Миша", "kid")
    assert "Аня" in _shown(callback)[0]


@pytest.mark.asyncio
async def test_other_adults_card_shows_who_decided(desk: Any) -> None:
    await _apply(desk)
    await _press_as(desk, f"apg:{NEW}", _actor("adult", ADULT))
    desk._bot.edit_message_text.assert_awaited_once()
    assert desk._bot.edit_message_text.await_args.kwargs["chat_id"] == OWNER
    assert "Аня" in desk._bot.edit_message_text.await_args.args[0]


@pytest.mark.asyncio
async def test_guest_is_permanent_and_has_no_pi_link(desk: Any, sessions: Any) -> None:
    await _apply(desk)
    await _press_as(desk, f"apg:{NEW}")
    member = await _member(sessions)
    assert member is not None and member.role == "guest" and member.homesec_person_id is None
    desk._pi.link_telegram.assert_not_awaited()


@pytest.mark.asyncio
async def test_two_adults_cannot_both_decide(desk: Any, sessions: Any) -> None:
    await _apply(desk)
    results = await asyncio.gather(
        _press_as(desk, f"apg:{NEW}"),
        _press_as(desk, f"apr:{NEW}", _actor("adult", ADULT)),
    )
    member, application = await _member(sessions), await _application(sessions)
    assert (member is None) != (application is None)
    assert any(_shown(callback)[0] == people.ALREADY_DECIDED for callback in results)


@pytest.mark.asyncio
async def test_rejection_erases_phone_and_tells_the_person(desk: Any, sessions: Any) -> None:
    await _apply(desk)
    await _press_as(desk, f"apr:{NEW}")
    application = await _application(sessions)
    assert application is not None and application.status == "rejected" and application.phone is None
    assert desk._bot.send_message.await_args.args[:2] == (NEW, people.TURNED_DOWN)


@pytest.mark.asyncio
async def test_pi_down_keeps_application_waiting_but_allows_guest(desk: Any, sessions: Any) -> None:
    await _apply(desk)
    desk._pi.people = AsyncMock(side_effect=PiUnavailableError("down"))
    callback = await _press_as(desk, f"apf:{NEW}:4")
    text, data = _shown(callback)
    assert people.NETWORK_DOWN in text and f"apg:{NEW}" in data
    assert await _member(sessions) is None and await _application(sessions) is not None
    await _press_as(desk, f"apg:{NEW}")
    member = await _member(sessions)
    assert member is not None and member.role == "guest"


@pytest.mark.asyncio
async def test_telegram_conflict_on_pi_admits_nobody(desk: Any, sessions: Any) -> None:
    await _apply(desk)
    desk._pi.link_telegram = AsyncMock(side_effect=PiConflictError("taken"))
    await _press_as(desk, f"apf:{NEW}:4")
    assert await _member(sessions) is None


@pytest.mark.asyncio
async def test_guest_moves_to_family(desk: Any, sessions: Any) -> None:
    await _apply(desk)
    await _press_as(desk, f"apg:{NEW}")
    assert f"pgf:{NEW}" in _shown(await _press_as(desk, f"pc:{NEW}", _actor("adult", ADULT)))[1]
    await _press_as(desk, f"pgp:{NEW}:3", _actor("adult", ADULT))
    member = await _member(sessions)
    assert member is not None and (member.role, member.homesec_person_id) == ("adult", 3)


@pytest.mark.asyncio
async def test_adult_can_remove_guest_who_then_cannot_reapply(desk: Any, sessions: Any) -> None:
    await _apply(desk)
    await _press_as(desk, f"apg:{NEW}")
    async with sessions() as db:
        db.add(AssistantMemory(member_id=NEW, content="факт", embedding=[0.1] * 1024))
        await db.commit()
    await _press_as(desk, f"pxy:{NEW}", _actor("adult", ADULT))
    assert await _member(sessions) is None
    application = await _application(sessions)
    assert application is not None and application.status == "rejected"
    async with sessions() as db:
        assert await db.scalar(select(AssistantMemory)) is None


@pytest.mark.asyncio
async def test_adult_cannot_remove_family(desk: Any, sessions: Any) -> None:
    async with sessions() as db:
        db.add(AssistantMember(telegram_id=NEW, name="Миша", role="kid", requests_today=0))
        await db.commit()
    await _press_as(desk, f"pxy:{NEW}", _actor("adult", ADULT))
    assert await _member(sessions) is not None
    assert f"px:{NEW}" not in _shown(await _press_as(desk, f"pc:{NEW}", _actor("adult", ADULT)))[1]


@pytest.mark.asyncio
async def test_owner_removes_family_but_not_themselves(desk: Any, sessions: Any) -> None:
    await _press_as(desk, f"pxy:{ADULT}")
    assert await _member(sessions, ADULT) is None
    await _press_as(desk, f"pxy:{OWNER}")
    assert await _member(sessions, OWNER) is not None


@pytest.mark.asyncio
async def test_rejected_person_can_be_given_another_chance(desk: Any, sessions: Any) -> None:
    await _apply(desk)
    await _press_as(desk, f"apr:{NEW}")
    callback = await _press_as(desk, f"prb:{NEW}", _actor("adult", ADULT))
    application = await _application(sessions)
    assert application is not None and application.status == "pending"
    assert f"apg:{NEW}" in _shown(callback)[1]


@pytest.mark.asyncio
async def test_summary_counts_every_state(desk: Any, sessions: Any) -> None:
    await _apply(desk)
    await _apply(desk, 501)
    await _press_as(desk, "apr:501")
    async with sessions() as db:
        db.add(AssistantMember(telegram_id=600, name="Гость", role="guest", requests_today=0))
        await db.commit()
    text, markup = await desk._summary()
    labels = [row[0].text for row in markup.inline_keyboard]
    assert text == "👥 Люди"
    assert labels == ["Семья: 2", "Гости: 1", "В ожидании: 1", "Отказано: 1"]


@pytest.mark.asyncio
async def test_old_pending_applications_expire_with_their_phone(desk: Any, sessions: Any) -> None:
    await _apply(desk)
    async with sessions() as db:
        application = await db.get(AssistantApplication, NEW)
        application.created_at = datetime.now(UTC) - timedelta(days=31)
        await db.commit()
        assert await access.expire_applications(db) == 1
    assert await _application(sessions) is None


@pytest.mark.asyncio
async def test_rejections_do_not_expire(desk: Any, sessions: Any) -> None:
    await _apply(desk)
    await _press_as(desk, f"apr:{NEW}")
    async with sessions() as db:
        application = await db.get(AssistantApplication, NEW)
        application.created_at = datetime.now(UTC) - timedelta(days=90)
        await db.commit()
        assert await access.expire_applications(db) == 0


@pytest.mark.asyncio
async def test_waiting_list_is_capped(desk: Any, monkeypatch: pytest.MonkeyPatch, sessions: Any) -> None:
    monkeypatch.setattr(access, "MAX_PENDING_APPLICATIONS", 1)
    await _apply(desk)
    await _apply(desk, 501)
    assert await _application(sessions, 501) is None
    assert desk._bot.send_message.await_args.args[1] == people.FULL


@pytest.mark.asyncio
async def test_flood_of_applications_stops_notifying(desk: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    for index in range(people.NOTIFICATIONS_PER_HOUR + 2):
        await _apply(desk, 1000 + index)
    to_owner = [chat for chat in _sent_to(desk) if chat == OWNER]
    assert len(to_owner) == people.NOTIFICATIONS_PER_HOUR


@pytest.mark.asyncio
async def test_invite_shares_only_the_bot_link(desk: Any) -> None:
    await desk.send_invite(OWNER)
    url = desk._bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard[0][0].url
    assert url.startswith("https://t.me/share/url?url=https%3A%2F%2Ft.me%2Flunabot&text=")
    assert "start" not in url.split("&text=")[0]


def test_callback_data_fits_telegram_limit() -> None:
    longest = f"apf:{10**15}:{10**9}"
    assert len(longest.encode()) <= 64
