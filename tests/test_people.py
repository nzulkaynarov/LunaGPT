"""The single way in and the adults' decisions, against a real database."""

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from aiogram.types import ForceReply, InlineKeyboardMarkup, Message, ReplyKeyboardRemove
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
                AssistantMember(telegram_id=OWNER, name="Глава", role="owner"),
                AssistantMember(telegram_id=ADULT, name="Аня", role="adult"),
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


PHONE = "+998901234567"


def _message(
    user_id: int = NEW, text: str = "/start", contact_user: int | None = None, phone: str = PHONE
) -> MagicMock:
    message = MagicMock(spec=Message, text=text)
    message.from_user = MagicMock(id=user_id, full_name="Новый", username="newbie")
    message.contact = (
        None if contact_user is None else MagicMock(user_id=contact_user, phone_number=phone, first_name="Н")
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
    assert application is not None and application.status == "pending" and application.phone == PHONE
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
        db.add(AssistantMember(telegram_id=9, name="Марат", role="adult", homesec_person_id=3))
        await db.commit()
    assert [p["id"] for p in await desk._free_people(NEW)] == [4]


@pytest.mark.asyncio
async def test_family_choice_takes_role_from_pi_and_keeps_phone(desk: Any, sessions: Any) -> None:
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
async def test_rejection_keeps_phone_and_tells_the_person(desk: Any, sessions: Any) -> None:
    await _apply(desk)
    await _press_as(desk, f"apr:{NEW}")
    application = await _application(sessions)
    assert application is not None and application.status == "rejected" and application.phone == PHONE
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
        db.add(AssistantMember(telegram_id=NEW, name="Миша", role="kid"))
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
        db.add(AssistantMember(telegram_id=600, name="Гость", role="guest"))
        await db.commit()
    text, markup = await desk._summary()
    labels = [row[0].text for row in markup.inline_keyboard]
    assert text == "👥 Люди"
    assert labels == ["Семья: 2", "Гости: 1", "В ожидании: 1", "Отказано: 1", "🔎 Найти по номеру"]


@pytest.mark.asyncio
async def test_old_application_expires_keeps_phone_and_person_may_reapply(desk: Any, sessions: Any) -> None:
    await _apply(desk)
    async with sessions() as db:
        application = await db.get(AssistantApplication, NEW)
        application.created_at = datetime.now(UTC) - timedelta(days=31)
        await db.commit()
        assert await access.expire_applications(db) == 1
    application = await _application(sessions)
    assert application is not None and application.status == "expired" and application.phone == PHONE
    await desk.on_stranger(_message(contact_user=NEW))
    application = await _application(sessions)
    assert application is not None and application.status == "pending"


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


OTHER_ACCOUNT = 501


async def _reject_and_retry_from_new_account(desk: Any, phone: str = "998901234567") -> None:
    await _apply(desk)
    await _press_as(desk, f"apr:{NEW}")
    desk._bot.send_message.reset_mock()
    await desk.on_stranger(_message(OTHER_ACCOUNT, contact_user=OTHER_ACCOUNT, phone=phone))


def _search_message(text: str = "", phone: str | None = None) -> MagicMock:
    message = MagicMock(spec=Message, text=text)
    message.contact = None if phone is None else MagicMock(phone_number=phone)
    return message


async def _search(desk: Any, text: str = "", phone: str | None = None) -> tuple[str, list[str], list[str]]:
    desk._bot.send_message.reset_mock()
    await desk.search(OWNER, _search_message(text, phone))
    call = desk._bot.send_message.await_args
    buttons = [b for row in call.kwargs["reply_markup"].inline_keyboard for b in row] if _is_inline(call) else []
    return call.args[1], [b.text for b in buttons], [b.callback_data for b in buttons]


def _is_inline(call: Any) -> bool:
    return isinstance(call.kwargs["reply_markup"], InlineKeyboardMarkup)


@pytest.mark.asyncio
async def test_new_account_with_rejected_number_stays_out_and_notifies_nobody(desk: Any, sessions: Any) -> None:
    await _reject_and_retry_from_new_account(desk)
    application = await _application(sessions, OTHER_ACCOUNT)
    assert application is not None and application.status == "rejected" and application.phone == PHONE
    desk._bot.send_message.assert_awaited_once()
    assert desk._bot.send_message.await_args.args[:2] == (OTHER_ACCOUNT, people.TURNED_DOWN)


@pytest.mark.asyncio
async def test_removed_member_keeps_number_and_new_account_is_refused(desk: Any, sessions: Any) -> None:
    await _apply(desk)
    await _press_as(desk, f"apg:{NEW}")
    member = await _member(sessions)
    assert member is not None and member.phone == PHONE
    await _press_as(desk, f"pxy:{NEW}")
    application = await _application(sessions)
    assert application is not None and application.status == "rejected" and application.phone == PHONE
    await desk.on_stranger(_message(OTHER_ACCOUNT, contact_user=OTHER_ACCOUNT))
    refused = await _application(sessions, OTHER_ACCOUNT)
    assert refused is not None and refused.status == "rejected"


@pytest.mark.asyncio
async def test_member_card_shows_phone(desk: Any) -> None:
    await _apply(desk)
    await _press_as(desk, f"apg:{NEW}")
    assert PHONE in _shown(await _press_as(desk, f"pc:{NEW}"))[0]


@pytest.mark.asyncio
async def test_search_finds_every_account_of_a_rejected_number(desk: Any) -> None:
    await _reject_and_retry_from_new_account(desk)
    text, labels, actions = await _search(desk, "90 123 45 67")
    assert PHONE in text
    assert f"prb:{NEW}" in actions and f"prb:{OTHER_ACCOUNT}" in actions
    assert any("отказано" in label for label in labels)


@pytest.mark.asyncio
async def test_search_accepts_a_shared_contact_and_finds_members(desk: Any) -> None:
    await _apply(desk)
    await _press_as(desk, f"apg:{NEW}")
    _, _, actions = await _search(desk, phone="998901234567")
    assert f"pc:{NEW}" in actions


@pytest.mark.asyncio
async def test_search_reports_an_unknown_number(desk: Any) -> None:
    text, _, actions = await _search(desk, "+998 91 000 00 00")
    assert "никого нет" in text and actions == ["pf", "pl"]


@pytest.mark.asyncio
async def test_search_asks_again_when_text_is_not_a_number(desk: Any) -> None:
    desk._bot.send_message.reset_mock()
    await desk.search(OWNER, _search_message("Миша"))
    call = desk._bot.send_message.await_args
    assert people.NOT_A_NUMBER in call.args[1] and isinstance(call.kwargs["reply_markup"], ForceReply)


@pytest.mark.asyncio
async def test_returning_a_number_forgives_it_on_every_account(desk: Any, sessions: Any) -> None:
    await _reject_and_retry_from_new_account(desk)
    await _press_as(desk, f"prb:{OTHER_ACCOUNT}")
    await _press_as(desk, f"apg:{OTHER_ACCOUNT}")
    member = await _member(sessions, OTHER_ACCOUNT)
    assert member is not None and member.role == "guest" and member.phone == PHONE
    assert await _application(sessions) is None


@pytest.mark.asyncio
async def test_expired_application_can_be_returned_from_search(desk: Any, sessions: Any) -> None:
    await _apply(desk)
    async with sessions() as db:
        application = await db.get(AssistantApplication, NEW)
        application.created_at = datetime.now(UTC) - timedelta(days=31)
        await db.commit()
        await access.expire_applications(db)
    _, labels, actions = await _search(desk, PHONE)
    assert f"prb:{NEW}" in actions and any("не рассмотрена" in label for label in labels)
    await _press_as(desk, f"prb:{NEW}")
    application = await _application(sessions)
    assert application is not None and application.status == "pending"


def test_only_a_reply_to_the_bot_prompt_is_a_search() -> None:
    reply = MagicMock(text=people.SEARCH_PROMPT, from_user=MagicMock(is_bot=True))
    assert PeopleDesk.is_search_reply(MagicMock(spec=Message, reply_to_message=reply))
    assert not PeopleDesk.is_search_reply(MagicMock(spec=Message, reply_to_message=None))
    forged = MagicMock(text=people.SEARCH_PROMPT, from_user=MagicMock(is_bot=False))
    assert not PeopleDesk.is_search_reply(MagicMock(spec=Message, reply_to_message=forged))


@pytest.mark.asyncio
async def test_search_button_opens_a_forced_reply(desk: Any) -> None:
    desk._bot.send_message.reset_mock()
    await _press_as(desk, "pf")
    call = desk._bot.send_message.await_args
    assert call.args == (OWNER, people.SEARCH_PROMPT) and isinstance(call.kwargs["reply_markup"], ForceReply)


async def _guest(desk: Any) -> None:
    await _apply(desk)
    await _press_as(desk, f"apg:{NEW}")


@pytest.mark.asyncio
async def test_guest_card_offers_limits_and_shows_none_by_default(desk: Any) -> None:
    await _guest(desk)
    text, labels = _shown(await _press_as(desk, f"pc:{NEW}"))
    assert "без срока" in text and "без лимита" in text
    assert f"pt:{NEW}" in labels and f"pm:{NEW}" in labels


@pytest.mark.asyncio
async def test_access_period_can_be_set_and_removed(desk: Any, sessions: Any) -> None:
    await _guest(desk)
    await _press_as(desk, f"pty:{NEW}:7")
    member = await _member(sessions)
    assert member is not None and member.access_until is not None
    assert timedelta(days=6) < member.access_until - datetime.now(UTC) <= timedelta(days=7)
    assert not access.access_ended(member)
    await _press_as(desk, f"pty:{NEW}:0")
    member = await _member(sessions)
    assert member is not None and member.access_until is None


@pytest.mark.asyncio
async def test_money_limit_counts_one_calendar_month(desk: Any, sessions: Any) -> None:
    await _guest(desk)
    await _press_as(desk, f"pmy:{NEW}:1")
    today = date(2026, 11, 20)
    async with sessions() as db:
        await access.record_spend(db, NEW, 0.6, today)
        await access.record_spend(db, NEW, 0.5, today)
    member = await _member(sessions)
    assert member is not None and member.money_limit_usd == Decimal(1)
    assert access.spent_this_month(member, today) == Decimal("1.1") and access.money_limit_reached(member, today)
    december = date(2026, 12, 1)
    assert access.spent_this_month(member, december) == 0 and not access.money_limit_reached(member, december)
    async with sessions() as db:
        await access.record_spend(db, NEW, 0.2, december)
    member = await _member(sessions)
    assert member is not None and access.spent_this_month(member, december) == Decimal("0.2")


@pytest.mark.asyncio
async def test_guest_settings_do_not_touch_family(desk: Any, sessions: Any) -> None:
    await _apply(desk)
    await _press_as(desk, f"apf:{NEW}:4")
    await _press_as(desk, f"pty:{NEW}:1")
    await _press_as(desk, f"pmy:{NEW}:1")
    member = await _member(sessions)
    assert member is not None and member.access_until is None and member.money_limit_usd is None


@pytest.mark.asyncio
async def test_moving_guest_to_family_clears_limits(desk: Any, sessions: Any) -> None:
    await _guest(desk)
    await _press_as(desk, f"pty:{NEW}:1")
    await _press_as(desk, f"pmy:{NEW}:1")
    await _press_as(desk, f"pgp:{NEW}:3")
    member = await _member(sessions)
    assert member is not None and member.role == "adult"
    assert member.access_until is None and member.money_limit_usd is None


@pytest.mark.asyncio
async def test_admitting_a_child_asks_the_adult_for_age(desk: Any, sessions: Any) -> None:
    await _apply(desk)
    desk._bot.send_message.reset_mock()
    await _press_as(desk, f"apf:{NEW}:4", _actor("adult", ADULT))
    call = desk._bot.send_message.await_args
    assert call.args[0] == ADULT and "Сколько лет" in call.args[1]
    data = [b.callback_data for row in call.kwargs["reply_markup"].inline_keyboard for b in row]
    assert f"pay:{NEW}:13" in data
    text, _ = _shown(await _press_as(desk, f"pay:{NEW}:13"))
    assert "Возраст: 13" in text
    member = await _member(sessions)
    assert member is not None and access.age_of(member, datetime.now(UTC).date()) == 13


@pytest.mark.asyncio
async def test_admitting_an_adult_does_not_ask_for_age(desk: Any) -> None:
    await _apply(desk)
    desk._bot.send_message.reset_mock()
    await _press_as(desk, f"apf:{NEW}:3")
    assert all("Сколько лет" not in str(call.args) for call in desk._bot.send_message.await_args_list)


def test_setting_callbacks_fit_telegram_limit() -> None:
    biggest = 9_999_999_999
    for data in (f"pay:{biggest}:17", f"pty:{biggest}:30", f"pmy:{biggest}:10"):
        assert len(data.encode()) <= 64
