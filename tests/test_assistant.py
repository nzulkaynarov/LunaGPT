"""Luna AI Assistant: access rules, routing and failure notices."""

import asyncio
import json
import uuid
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
    return AssistantMember(telegram_id=telegram_id, name="Тест", role=role, requests_today=0)


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
    await instance.on_message(_message(bot.BTN_PEOPLE))
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


@pytest.mark.parametrize(
    ("role", "expected", "hidden"),
    [
        ("owner", bot.BTN_PEOPLE, None),
        ("adult", bot.BTN_INVITE, bot.BTN_QUIZ),
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
        ("owner", "pxy", True),
        ("adult", "shs", True),
        ("adult", "pxy", True),
        ("owner", "link", False),
        ("kid", "shs", False),
        ("kid", "quiz", True),
        ("guest", "pl", False),
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


def _memory_fact(index: int) -> Any:
    return MagicMock(id=uuid.UUID(int=index), content=f"факт {index}")


def _with_memory(harness: Any, monkeypatch: pytest.MonkeyPatch, role: str, count: int = 0) -> tuple[bot.LunaBot, Any]:
    instance, _, _ = harness
    _as_member(monkeypatch, _member(role))
    memory = MagicMock(
        recall=AsyncMock(return_value=["Пользователь любит игры"]),
        load_history=AsyncMock(return_value=[{"role": "user", "content": "раньше"}]),
        save_exchange=AsyncMock(),
        remember=AsyncMock(),
        list_facts=AsyncMock(return_value=[_memory_fact(i) for i in range(count)]),
        forget=AsyncMock(return_value=True),
        forget_all=AsyncMock(),
    )
    instance._memory = memory
    return instance, memory


@pytest.mark.asyncio
async def test_reply_uses_facts_and_restored_history(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, memory = _with_memory(harness, monkeypatch, "owner")
    _, _, answer = harness
    await instance.on_message(_message("вопрос про игры"))
    args = answer.call_args.args
    assert args[2] == [{"role": "user", "content": "раньше"}] and args[4] == ["Пользователь любит игры"]
    memory.save_exchange.assert_awaited_once_with(1, "вопрос про игры", "ответ", bot.HISTORY_MESSAGES)
    await asyncio.sleep(0)
    memory.remember.assert_awaited_once_with(1, "owner", "вопрос про игры")


@pytest.mark.asyncio
async def test_history_loads_from_database_only_once(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, memory = _with_memory(harness, monkeypatch, "owner")
    await instance.on_message(_message("первый"))
    await instance.on_message(_message("второй"))
    memory.load_history.assert_awaited_once()


@pytest.mark.asyncio
async def test_kid_danger_message_is_not_remembered(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, memory = _with_memory(harness, monkeypatch, "kid")
    monkeypatch.setattr(access, "adult_chat_ids", AsyncMock(return_value=[]))
    await instance.on_message(_message("меня бьют в школе"))
    await asyncio.sleep(0)
    memory.remember.assert_not_awaited()


@pytest.mark.asyncio
async def test_memory_view_pages_and_delete_buttons(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, _ = _with_memory(harness, monkeypatch, "owner", count=10)
    text, markup = await instance._memory_view(1, 1)
    assert "9. факт 8" in text and "(10)" in text
    data = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert all(len(item.encode()) <= 64 for item in data)
    assert "mem:0" in data and "mem:2" not in data and "memc" in data


@pytest.mark.asyncio
async def test_delete_button_is_scoped_to_the_presser(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, memory = _with_memory(harness, monkeypatch, "kid", count=1)
    target = uuid.UUID(int=5)
    await instance.on_callback(_callback(f"memd:{target.hex}:0"))
    memory.forget.assert_awaited_once_with(1, target)


@pytest.mark.asyncio
async def test_clear_all_asks_first(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, memory = _with_memory(harness, monkeypatch, "owner", count=1)
    callback = _callback("memc")
    await instance.on_callback(callback)
    memory.forget_all.assert_not_awaited()
    assert "memy" in str(callback.message.edit_text.await_args.kwargs["reply_markup"])
    await instance.on_callback(_callback("memy"))
    memory.forget_all.assert_awaited_once_with(1)


@pytest.mark.parametrize("role", ["owner", "adult", "kid", "guest"])
def test_memory_button_is_for_every_member(role: str) -> None:
    assert bot.BTN_MEMORY in str(bot.main_keyboard(role).keyboard)
    assert bot.LunaBot._callback_allowed(role, "memy") is True


@pytest.mark.asyncio
async def test_guest_conversation_is_remembered_like_anyone_elses(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, memory = _with_memory(harness, monkeypatch, "guest")
    await instance.on_message(_message("расскажи про горы"))
    memory.save_exchange.assert_awaited_once()
    await asyncio.sleep(0)
    memory.remember.assert_awaited_once_with(1, "guest", "расскажи про горы")


def test_guests_start_on_the_local_model() -> None:
    assert llm.MODELS_BY_ROLE["guest"][0] == "luna-local"


@pytest.mark.parametrize("role", ["kid", "guest"])
@pytest.mark.parametrize("prefix", ["pl", "pls", "pc", "apf", "apg", "apr", "pxy", "prb"])
def test_people_buttons_are_for_adults(role: str, prefix: str) -> None:
    assert bot.LunaBot._callback_allowed(role, prefix) is False
    assert bot.LunaBot._callback_allowed("adult", prefix) is True


@pytest.mark.asyncio
async def test_real_dispatcher_turns_a_stranger_into_an_applicant(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _, _ = harness
    _as_member(monkeypatch, None)
    on_stranger = AsyncMock()
    instance._people.on_stranger = on_stranger
    dispatcher = Dispatcher()
    dispatcher.include_router(bot.build_router(instance))
    telegram = Bot("123:abc")
    update = Update.model_validate(
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 1,
                "chat": {"id": 1, "type": "private"},
                "from": {"id": 1, "is_bot": False, "first_name": "A"},
                "contact": {"phone_number": "+998", "first_name": "A", "user_id": 1},
            },
        }
    )
    await dispatcher.feed_update(telegram, update)
    on_stranger.assert_awaited_once()
    await telegram.session.close()
