"""Luna AI Assistant: access rules, routing and failure notices."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Message, Update

from lunagpt.assistant import access, bot, llm
from lunagpt.assistant.monitor import AvailabilityTracker
from lunagpt.assistant.pi_client import PiClient, PiConflictError, PiNotFoundError, PiUnavailableError
from lunagpt.assistant.telegram import split_message
from lunagpt.models import AssistantMember


def _member(role: str = "adult", telegram_id: int = 1) -> AssistantMember:
    return AssistantMember(telegram_id=telegram_id, name="Тест", role=role, expires_at=None, requests_today=0)


def _message(text: str, user_id: int = 1, chat_type: str = "private") -> Any:
    message = MagicMock()
    message.chat.type = chat_type
    message.from_user.id = user_id
    message.text = text
    return message


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> tuple[bot.LunaBot, AsyncMock, AsyncMock]:
    telegram = MagicMock(send_message=AsyncMock(), send_chat_action=AsyncMock())
    answer = AsyncMock(return_value="ответ")
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=MagicMock())
    session.__aexit__ = AsyncMock(return_value=None)
    instance = bot.LunaBot(telegram, MagicMock(return_value=session), answer, AsyncMock(), MagicMock(), "lunabot")
    monkeypatch.setattr(access, "consume_daily_request", AsyncMock(return_value=True))
    return instance, cast(AsyncMock, telegram.send_message), answer


def _as_member(monkeypatch: pytest.MonkeyPatch, member: AssistantMember | None) -> None:
    monkeypatch.setattr(access, "get_member", AsyncMock(return_value=member))


def _sent(send: AsyncMock) -> str:
    return str(send.call_args.args[1])


QUIZ = llm.QuizQuestion(q="Сколько планет?", options=["7", "8", "9", "10"], correct=1, topic="Космос")


@pytest.mark.asyncio
async def test_stranger_is_rejected_once(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, send, answer = harness
    _as_member(monkeypatch, None)
    await instance.on_message(_message("привет"))
    await instance.on_message(_message("привет"))
    answer.assert_not_awaited()
    assert send.await_count == 1 and _sent(send) == bot.NOT_INVITED


@pytest.mark.asyncio
async def test_group_messages_are_ignored(harness: Any) -> None:
    instance, send, answer = harness
    await instance.on_message(_message("привет", chat_type="group"))
    answer.assert_not_awaited()
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_member_gets_answer_and_history(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, send, answer = harness
    _as_member(monkeypatch, _member())
    await instance.on_message(_message("вопрос"))
    assert _sent(send) == "ответ"
    await instance.on_message(_message("ещё"))
    assert answer.call_args.args[2] == [
        {"role": "user", "content": "вопрос"},
        {"role": "assistant", "content": "ответ"},
    ]


@pytest.mark.asyncio
async def test_kid_danger_message_alerts_parents_without_model(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, send, answer = harness
    _as_member(monkeypatch, _member("kid", 5))
    monkeypatch.setattr(access, "adult_chat_ids", AsyncMock(return_value=[9]))
    await instance.on_message(_message("меня бьют в школе", 5))
    answer.assert_not_awaited()
    assert send.await_args_list[0].args[0] == 9
    assert send.await_args_list[1].args[:2] == (5, bot.SAFETY_REPLY)


@pytest.mark.asyncio
async def test_guest_cannot_use_owner_commands(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, _, answer = harness
    _as_member(monkeypatch, _member("guest"))
    list_members = AsyncMock()
    monkeypatch.setattr(access, "list_members", list_members)
    await instance.on_message(_message(bot.BTN_MEMBERS))
    list_members.assert_not_awaited()
    answer.assert_awaited_once()  # the text is treated as an ordinary question


def _state(current: str | None = None, data: dict[str, Any] | None = None) -> MagicMock:
    state = MagicMock(
        get_state=AsyncMock(return_value=current),
        set_state=AsyncMock(),
        update_data=AsyncMock(),
        get_data=AsyncMock(return_value=data or {}),
        clear=AsyncMock(),
    )
    return state


def _callback(data: str, user_id: int = 1) -> MagicMock:
    callback = MagicMock(data=data, answer=AsyncMock())
    callback.from_user.id = user_id
    callback.message = MagicMock(spec=Message, edit_text=AsyncMock())
    return callback


@pytest.mark.asyncio
async def test_guest_button_asks_name_then_duration(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, send, _ = harness
    _as_member(monkeypatch, _member("adult"))
    state = _state()
    await instance.on_message(_message(bot.BTN_GUEST), state)
    state.set_state.assert_awaited_once_with(bot.GuestInvite.name)
    assert send.call_args.kwargs["reply_markup"].force_reply is True
    naming = _state(bot.GuestInvite.name.state)
    await instance.on_message(_message("Анна Ли"), naming)
    naming.update_data.assert_awaited_once_with(guest_name="Анна Ли")
    assert send.call_args.kwargs["reply_markup"].inline_keyboard[0][1].callback_data == "guest:6"


@pytest.mark.asyncio
async def test_pressing_a_menu_button_cancels_guest_naming(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, _, answer = harness
    _as_member(monkeypatch, _member("adult"))
    naming = _state(bot.GuestInvite.name.state)
    await instance.on_message(_message(bot.BTN_HELP), naming)
    naming.update_data.assert_not_awaited()
    answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_guest_duration_creates_link_in_same_message(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, _, _ = harness
    _as_member(monkeypatch, _member("adult"))
    create = AsyncMock(return_value="TOKEN")
    monkeypatch.setattr(access, "create_invite", create)
    callback = _callback("guest:6")
    await instance.on_callback(callback, _state(data={"guest_name": "Анна"}))
    assert create.call_args.args[1:] == ("Анна", "guest", 6)
    assert "https://t.me/lunabot?start=TOKEN" in callback.message.edit_text.call_args.args[0]


@pytest.mark.parametrize(
    ("role", "expected", "hidden"),
    [
        ("owner", bot.BTN_FAMILY, None),
        ("adult", bot.BTN_SHARED, bot.BTN_FAMILY),
        ("kid", bot.BTN_ASK_TIME, bot.BTN_SHARED),
        ("guest", bot.BTN_HELP, bot.BTN_SHARED),
    ],
)
def test_menu_matches_role(role: str, expected: str, hidden: str | None) -> None:
    keyboard = bot.main_keyboard(role)
    texts = [button.text for row in keyboard.keyboard for button in row]
    assert expected in texts and hidden not in texts
    assert keyboard.is_persistent is True and keyboard.resize_keyboard is True


@pytest.mark.parametrize(
    ("role", "prefix", "allowed"),
    [
        ("owner", "link", True),
        ("owner", "rmy", True),
        ("adult", "link", False),
        ("adult", "rmy", False),
        ("adult", "shs", True),
        ("adult", "guest", True),
        ("kid", "shs", False),
        ("kid", "quiz", True),
        ("guest", "guest", False),
        ("guest", "sh", False),
        ("owner", "unknown", False),
    ],
)
def test_callback_permissions(role: str, prefix: str, allowed: bool) -> None:
    assert bot.LunaBot._callback_allowed(role, prefix) is allowed


def test_callback_data_fits_telegram_limit() -> None:
    for row in bot.shared_actions_keyboard(10**12).inline_keyboard:
        assert all(len((button.callback_data or "").encode()) <= 64 for button in row)


@pytest.mark.asyncio
async def test_kid_cannot_open_shared_devices_by_typing(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, _, answer = harness
    _as_member(monkeypatch, _member("kid", 5))
    instance._pi.shared_devices = AsyncMock(return_value=[])
    await instance.on_message(_message(bot.BTN_SHARED, 5))
    instance._pi.shared_devices.assert_not_awaited()
    answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_shared_device_buttons_edit_same_message(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, _, _ = harness
    _as_member(monkeypatch, _member("adult"))
    device = {"id": 7, "name": "TV", "state": "adult", "until": "2026-11-09T23:30:00"}
    instance._pi.set_shared_state = AsyncMock(return_value=device)
    callback = _callback("shs:7:3")
    await instance.on_callback(callback)
    instance._pi.set_shared_state.assert_awaited_once_with(7, "3")
    callback.answer.assert_awaited_once_with()
    assert "до 23:30" in callback.message.edit_text.call_args.args[0]


@pytest.mark.asyncio
async def test_kid_is_denied_shared_button_press(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, _, _ = harness
    _as_member(monkeypatch, _member("kid", 5))
    instance._pi.set_shared_state = AsyncMock()
    await instance.on_callback(_callback("shs:7:off", 5))
    instance._pi.set_shared_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_member_removal_needs_confirmation(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, _, _ = harness
    _as_member(monkeypatch, _member("owner"))
    remove = AsyncMock(return_value=True)
    monkeypatch.setattr(access, "remove_member", remove)
    monkeypatch.setattr(access, "list_members", AsyncMock(return_value=[]))
    await instance.on_callback(_callback("rm:9"))
    remove.assert_not_awaited()
    await instance.on_callback(_callback("rmy:9"))
    remove.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("choice", "body"),
    [
        ("off", {"state": "off"}),
        ("kid", {"state": "kid"}),
        ("3", {"state": "adult", "hours": 3.0}),
        ("m", {"state": "adult", "until_morning": True}),
    ],
)
async def test_pi_client_maps_shared_choices(choice: str, body: dict[str, Any]) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": 7, "name": "TV", "state": "kid", "until": None})

    await _pi_client(handler).set_shared_state(7, choice)
    assert seen["body"] == body


@pytest.mark.parametrize(
    ("inviter", "role", "allowed"),
    [
        ("owner", "kid", True),
        ("adult", "guest", True),
        ("adult", "kid", False),
        ("kid", "guest", False),
        ("guest", "guest", False),
    ],
)
def test_invite_permissions(inviter: str, role: str, allowed: bool) -> None:
    assert access.can_invite(inviter, role) is allowed


def test_kids_use_the_same_models_as_adults_with_strict_search() -> None:
    assert llm.MODELS_BY_ROLE["kid"] == llm.MODELS_BY_ROLE["adult"]
    assert "kid" in llm.SEARCH_ROLES
    assert "kid" not in access.DAILY_REQUEST_LIMITS


def test_monitor_ignores_short_outage_and_reports_once() -> None:
    tracker = AvailabilityTracker(failures_before_alert=3)
    assert [tracker.observe(False), tracker.observe(False), tracker.observe(True)] == [None, None, None]
    assert [tracker.observe(False) for _ in range(4)] == [None, None, "down", None]
    assert tracker.observe(True) == "up"
    assert tracker.observe(True) is None


def test_split_message() -> None:
    assert all(len(p) <= 100 for p in split_message("a" * 350, 100))
    assert split_message("short") == ["short"]


@pytest.mark.asyncio
async def test_failed_model_falls_through(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def fake_run(client: Any, model: str, *args: Any) -> str:
        calls.append(model)
        if model == "luna-local":
            raise ValueError("bad tool call")
        return "готово"

    monkeypatch.setattr(llm, "_run_model", fake_run)
    assert await llm.generate_reply("adult", 1, [], "вопрос") == "готово"
    assert calls == ["luna-local", "luna-fast"]


def _kid_with_pi(harness: Any, monkeypatch: pytest.MonkeyPatch) -> tuple[bot.LunaBot, AsyncMock, MagicMock]:
    instance, send, _ = harness
    _as_member(monkeypatch, _member("kid", 5))
    monkeypatch.setattr(access, "track_time_request", AsyncMock())
    return instance, send, instance._pi


@pytest.mark.asyncio
async def test_time_request_offers_quiz(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, send, pi = _kid_with_pi(harness, monkeypatch)
    pi.request_time = AsyncMock(return_value=7)
    pi.quiz_available = AsyncMock(return_value=True)
    await instance.on_message(_message(bot.BTN_ASK_TIME, 5))
    access.track_time_request.assert_awaited()
    assert bot.BTN_QUIZ in _sent(send)


@pytest.mark.asyncio
async def test_ordinary_kid_message_never_mentions_quiz(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, send, _ = _kid_with_pi(harness, monkeypatch)
    await instance.on_message(_message("расскажи про космос", 5))
    instance._make_quiz.assert_not_awaited()
    assert _sent(send) == "ответ"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (PiConflictError("x"), "уже отправлен"),
        (PiNotFoundError("x"), "устройство"),
        (PiUnavailableError("x"), "не отвечает"),
    ],
)
async def test_time_request_failures_are_explained(
    harness: Any, monkeypatch: pytest.MonkeyPatch, error: Exception, expected: str
) -> None:
    instance, send, pi = _kid_with_pi(harness, monkeypatch)
    pi.request_time = AsyncMock(side_effect=error)
    await instance.on_message(_message(bot.BTN_ASK_TIME, 5))
    assert expected in _sent(send)


@pytest.mark.asyncio
async def test_quiz_correct_answer_awards_minutes(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, send, pi = _kid_with_pi(harness, monkeypatch)
    pi.quiz_available = AsyncMock(return_value=True)
    pi.quiz_result = AsyncMock(return_value=15)
    instance._make_quiz = AsyncMock(return_value=QUIZ)
    await instance.on_message(_message(bot.BTN_QUIZ, 5))
    assert send.call_args.kwargs["reply_markup"].inline_keyboard[1][0].callback_data == "quiz:1"
    callback = MagicMock(data="quiz:1", answer=AsyncMock())
    callback.from_user.id = 5
    await instance.on_callback(callback)
    pi.quiz_result.assert_awaited_once_with("Тест", True, "Космос")
    assert "+15" in _sent(send)
    await instance.on_callback(callback)
    pi.quiz_result.assert_awaited_once()


@pytest.mark.asyncio
async def test_stranger_cannot_answer_quiz(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, _, _ = harness
    _as_member(monkeypatch, None)
    callback = MagicMock(data="quiz:1", answer=AsyncMock())
    callback.from_user.id = 9
    await instance.on_callback(callback)
    callback.answer.assert_awaited_once_with("Нет доступа", show_alert=True)


PEOPLE = [
    {"id": 3, "name": "Миша", "role": "kid", "devices": 2, "telegram_id": None},
    {"id": 4, "name": "Мама", "role": "adult", "devices": 1, "telegram_id": 44},
]


@pytest.mark.asyncio
async def test_family_lists_people_from_pi(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, send, _ = harness
    _as_member(monkeypatch, _member("owner"))
    instance._pi.people = AsyncMock(return_value=PEOPLE)
    await instance.on_message(_message(bot.BTN_FAMILY))
    rows = send.call_args.kwargs["reply_markup"].inline_keyboard
    assert rows[0][0].callback_data == "link:3" and rows[0][0].text.startswith("➕ Миша")
    assert rows[1][0].text.startswith("✅ Мама")


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["adult", "kid", "guest"])
async def test_only_owner_opens_family(harness: Any, monkeypatch: pytest.MonkeyPatch, role: str) -> None:
    instance, _, _ = harness
    _as_member(monkeypatch, _member(role))
    instance._pi.people = AsyncMock(return_value=PEOPLE)
    await instance.on_message(_message(bot.BTN_FAMILY))
    instance._pi.people.assert_not_awaited()


@pytest.mark.asyncio
async def test_owner_gets_link_with_role_from_pi(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, send, _ = harness
    _as_member(monkeypatch, _member("owner"))
    instance._pi.people = AsyncMock(return_value=PEOPLE)
    create = AsyncMock(return_value="TOKEN")
    monkeypatch.setattr(access, "create_invite", create)
    callback = _callback("link:3")
    await instance.on_callback(callback)
    assert create.call_args.args[1:] == ("Миша", "kid") and create.call_args.kwargs == {"person_id": 3}
    assert "start=TOKEN" in _sent(send)


@pytest.mark.asyncio
async def test_non_owner_cannot_create_family_link(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, _, _ = harness
    _as_member(monkeypatch, _member("adult"))
    create = AsyncMock()
    monkeypatch.setattr(access, "create_invite", create)
    callback = MagicMock(data="link:3", answer=AsyncMock())
    callback.from_user.id = 1
    await instance.on_callback(callback)
    create.assert_not_awaited()
    callback.answer.assert_awaited_once_with("Нет доступа", show_alert=True)


@pytest.mark.asyncio
async def test_redeeming_family_link_writes_telegram_to_pi(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, _, _ = harness
    member = _member("kid", 5)
    member.homesec_person_id = 3
    monkeypatch.setattr(access, "redeem_invite", AsyncMock(return_value=member))
    instance._pi.link_telegram = AsyncMock()
    await instance.on_message(_message("/start TOKEN", 5))
    instance._pi.link_telegram.assert_awaited_once_with(3, 5)


def _db_with_members(members: list[AssistantMember]) -> MagicMock:
    db = MagicMock(commit=AsyncMock(), delete=AsyncMock())
    return db


@pytest.mark.asyncio
async def test_reconcile_follows_pi_and_removes_deleted_people(monkeypatch: pytest.MonkeyPatch) -> None:
    renamed = _member("guest", 5)
    renamed.homesec_person_id = 3
    removed = _member("kid", 6)
    removed.homesec_person_id = 99
    owner = _member("owner", 7)
    owner.homesec_person_id = 98
    linked = _member("adult", 44)
    linked.homesec_person_id = 4
    unlinked_guest = _member("guest", 8)
    monkeypatch.setattr(
        access, "list_members", AsyncMock(return_value=[renamed, removed, owner, linked, unlinked_guest])
    )
    db = _db_with_members([])
    assert await access.reconcile_family(db, PEOPLE) == [(3, 5)]
    assert (renamed.name, renamed.role) == ("Миша", "kid")
    db.delete.assert_awaited_once_with(removed)  # the owner survives a missing profile
    assert unlinked_guest.name == "Тест"


@pytest.mark.asyncio
async def test_decision_is_reported_and_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    telegram = MagicMock(send_message=AsyncMock())
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=MagicMock())
    session.__aexit__ = AsyncMock(return_value=None)
    request = MagicMock(request_id=3, telegram_id=5)
    monkeypatch.setattr(access, "pending_time_requests", AsyncMock(return_value=[request]))
    finish = AsyncMock()
    monkeypatch.setattr(access, "finish_time_request", finish)
    pi = MagicMock(time_request_status=AsyncMock(return_value={"status": "approved", "minutes": 30}))
    await bot.report_time_decisions(telegram, pi, MagicMock(return_value=session))
    assert "30 минут" in telegram.send_message.call_args.args[1]
    finish.assert_awaited_once()


def _pi_client(handler: Any) -> PiClient:
    return PiClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_pi_client_encodes_cyrillic_and_sends_token(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.raw_path.decode()
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": 11, "status": "pending"})

    assert await _pi_client(handler).request_time("Миша К", "хочу") == 11
    assert seen["path"].endswith("/people/%D0%9C%D0%B8%D1%88%D0%B0%20%D0%9A/time-requests")
    assert seen["body"] == {"reason": "хочу"}


@pytest.mark.asyncio
@pytest.mark.parametrize(("code", "error"), [(404, PiNotFoundError), (409, PiConflictError), (500, PiUnavailableError)])
async def test_pi_client_maps_errors(code: int, error: type[Exception]) -> None:
    with pytest.raises(error):
        await _pi_client(lambda request: httpx.Response(code, json={})).quota("Миша")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [httpx.Response(302, headers={"location": "/login"}), httpx.Response(200, text="<html>not json</html>")],
)
async def test_pi_without_api_is_up_but_api_calls_fail(response: httpx.Response) -> None:
    client = _pi_client(lambda request: response)
    assert await client.is_healthy() is True  # the Pi answered, so it is up
    with pytest.raises(PiUnavailableError):
        await client.heartbeat([1])


@pytest.mark.asyncio
async def test_owner_keeps_role_when_linking_own_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    invite = MagicMock(used_at=None, access_hours=None, role="adult", homesec_person_id=4)
    invite.name = "Нурбек"
    invite.invite_expires_at = datetime.now(UTC) + timedelta(hours=1)
    owner = _member("owner", 7)
    db = MagicMock(commit=AsyncMock(), rollback=AsyncMock(), execute=AsyncMock(), get=AsyncMock(return_value=owner))
    db.execute.return_value.scalar_one_or_none = MagicMock(return_value=invite)
    member = await access.redeem_invite(db, "token", 7)
    assert member is owner and owner.role == "owner" and owner.homesec_person_id == 4


@pytest.mark.asyncio
async def test_pi_server_error_is_unhealthy() -> None:
    assert await _pi_client(lambda request: httpx.Response(503, json={})).is_healthy() is False


@pytest.mark.asyncio
async def test_pi_client_reports_unreachable_as_unhealthy() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    assert await _pi_client(handler).is_healthy() is False


@pytest.mark.parametrize(
    "raw",
    [
        '{"q": "x"}',
        '{"q": "Вопрос?", "options": ["a"], "correct": 0}',
        '{"q": "Вопрос?", "options": ["a","b","c","d"], "correct": 9}',
    ],
)
def test_malformed_quiz_is_rejected(raw: str) -> None:
    with pytest.raises(ValueError):
        llm.QuizQuestion.model_validate_json(raw)


@pytest.mark.asyncio
async def test_quiz_uses_a_varied_topic_on_cloud_models(monkeypatch: pytest.MonkeyPatch) -> None:
    payloads: list[dict[str, Any]] = []

    async def fake_chat(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
        payloads.append(payload)
        return {
            "content": '```json\n{"q": "Вопрос тут?", "options": ["a","b","c","d"], "correct": 2, "topic": "t"}\n```'
        }

    monkeypatch.setattr(llm, "_chat", fake_chat)
    monkeypatch.setattr(llm.random, "choice", lambda topics: "космос")
    quiz = await llm.generate_quiz(1)
    assert quiz is not None and quiz.correct == 2
    assert payloads[0]["model"] == "luna-fast"
    assert "космос" in payloads[0]["messages"][1]["content"]


@pytest.mark.asyncio
async def test_real_dispatcher_runs_guest_flow(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """aiogram itself must inject FSM state into the handlers."""
    instance, send, _ = harness
    _as_member(monkeypatch, _member("adult"))
    dispatcher = Dispatcher()
    dispatcher.include_router(bot.build_router(instance))
    telegram = Bot("123456:" + "a" * 35)

    def update(update_id: int, text: str) -> Update:
        return Update.model_validate(
            {
                "update_id": update_id,
                "message": {
                    "message_id": update_id,
                    "date": 1,
                    "chat": {"id": 1, "type": "private"},
                    "from": {"id": 1, "is_bot": False, "first_name": "A"},
                    "text": text,
                },
            }
        )

    await dispatcher.feed_update(telegram, update(1, bot.BTN_GUEST))
    assert send.call_args.args[1] == "Как зовут гостя?"
    await dispatcher.feed_update(telegram, update(2, "Анна"))
    assert send.call_args.args[1] == "На сколько пустить: Анна?"
    await telegram.session.close()
