"""Luna AI Assistant: access rules, routing and failure notices."""

import asyncio
import io
import json
import math
import uuid
import wave
from array import array
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import av
import httpx
import pytest
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message, Update

from lunagpt.assistant import access, audio, bot, llm, speech
from lunagpt.assistant.monitor import AvailabilityTracker
from lunagpt.assistant.pi_client import PiClient, PiConflictError, PiNotFoundError, PiUnavailableError
from lunagpt.assistant.telegram import split_message
from lunagpt.models import AssistantMember


def _member(role: str = "adult", telegram_id: int = 1) -> AssistantMember:
    return AssistantMember(telegram_id=telegram_id, name="Тест", role=role)


def _message(text: str, user_id: int = 1, chat_type: str = "private") -> Any:
    message = MagicMock()
    message.chat.type = chat_type
    message.from_user.id = user_id
    message.text = text
    message.voice = None
    message.video_note = None
    message.video = None
    return message


def _voice_message(duration: int = 5) -> Any:
    message = _message("")
    message.text = None
    message.voice = MagicMock(duration=duration)
    return message


def _hearing(instance: bot.LunaBot, words: str | None) -> AsyncMock:
    """Give the bot a downloaded voice note and a transcriber that hears the given words."""
    transcribe = AsyncMock(return_value=words)
    instance._transcribe = transcribe
    cast(Any, instance._bot).download = AsyncMock(return_value=io.BytesIO(b"ogg"))
    cast(Any, instance._bot).send_voice = AsyncMock()
    instance._speak = AsyncMock(return_value=speech.SpokenReply(b"voice", 3))
    return transcribe


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> tuple[bot.LunaBot, AsyncMock, AsyncMock]:
    telegram = MagicMock(send_message=AsyncMock(), send_chat_action=AsyncMock())

    async def shown_like_text(chat_id: int, rich: Any, reply_markup: Any = None) -> None:
        await telegram.send_message(chat_id, rich.markdown, reply_markup=reply_markup)

    telegram.send_rich_message = shown_like_text
    answer = AsyncMock(return_value="ответ")
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=MagicMock())
    session.__aexit__ = AsyncMock(return_value=None)
    instance = bot.LunaBot(telegram, MagicMock(return_value=session), answer, AsyncMock(), MagicMock(), "lunabot")
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
async def test_voice_is_shown_and_answered_like_text(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, send, answer = harness
    _as_member(monkeypatch, _member())
    transcribe = _hearing(instance, "какая погода")
    await instance.on_message(_voice_message())
    transcribe.assert_awaited_once_with("adult", 1, b"ogg", None, False)
    assert send.await_args_list[0].args[1] == "🗣 «какая погода»"
    assert answer.call_args.args[3] == "какая погода"
    assert _sent(send) == "ответ"


@pytest.mark.asyncio
async def test_voice_question_gets_text_then_voice(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, send, _ = harness
    _as_member(monkeypatch, _member())
    _hearing(instance, "какая погода")
    await instance.on_message(_voice_message())
    assert _sent(send) == "ответ"
    instance._speak.assert_awaited_once_with("ответ")
    voice = instance._bot.send_voice.await_args
    assert voice.args[0] == 1
    assert voice.args[1].data == b"voice"
    assert voice.kwargs["duration"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["ответь голосом, какая погода", "Say out loud why the sky is blue"])
async def test_typed_request_for_voice_is_voiced(harness: Any, monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    instance, _, answer = harness
    _as_member(monkeypatch, _member())
    _hearing(instance, None)
    await instance.on_message(_message(text))
    instance._speak.assert_awaited_once_with("ответ")
    assert answer.call_args.args[5].spoken is True


@pytest.mark.asyncio
async def test_bare_voice_request_says_the_last_answer_without_a_model(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _, answer = harness
    _as_member(monkeypatch, _member())
    _hearing(instance, None)
    await instance.on_message(_message("вопрос"))
    answer.reset_mock()
    await instance.on_message(_message("ответь голосом"))
    answer.assert_not_awaited()
    instance._speak.assert_awaited_once_with("ответ")


@pytest.mark.asyncio
async def test_bare_voice_request_with_nothing_to_say(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, send, _ = harness
    _as_member(monkeypatch, _member())
    _hearing(instance, None)
    await instance.on_message(_message("скажи голосом"))
    assert _sent(send) == bot.NOTHING_TO_SAY


@pytest.mark.asyncio
async def test_unreadable_last_answer_is_explained(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, send, _ = harness
    _as_member(monkeypatch, _member())
    _hearing(instance, None)
    instance._speak = AsyncMock(return_value=None)
    await instance.on_message(_message("вопрос"))
    await instance.on_message(_message("ответь голосом"))
    assert _sent(send) == bot.CANNOT_SAY


@pytest.mark.parametrize(
    ("text", "bare"),
    [
        ("ответь голосом", True),
        ("Скажи это вслух, пожалуйста", True),
        ("say it out loud", True),
        ("сделай этот текст голосом", True),
        ("озвучь этот ответ", True),
        ("расскажи голосом сказку", False),
        ("ответь голосом, какая погода в ташкенте", False),
        ("какая погода", False),
    ],
)
def test_bare_voice_request_is_told_from_a_question(text: str, bare: bool) -> None:
    assert speech.is_bare_voice_request(text) is bare


@pytest.mark.parametrize(
    ("text", "question"),
    [
        ("расскажи голосом сказку", "расскажи сказку"),
        ("ответь голосом, какая погода", "ответь , какая погода"),
        ("Tell me a story out loud", "Tell me a story"),
    ],
)
def test_the_model_never_sees_the_request_to_speak(text: str, question: str) -> None:
    assert speech.without_voice_words(text) == question


@pytest.mark.asyncio
async def test_typed_voice_request_reaches_the_model_without_voice_words(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _, answer = harness
    _as_member(monkeypatch, _member())
    _hearing(instance, None)
    await instance.on_message(_message("расскажи голосом сказку"))
    assert answer.call_args.args[3] == "расскажи сказку"
    instance._speak.assert_awaited_once_with("ответ")


def test_past_voice_refusals_are_dropped_from_history() -> None:
    history = [
        {"role": "user", "content": "ответь голосом"},
        {"role": "assistant", "content": "Я не могу ответить голосом. 😔"},
        {"role": "user", "content": "привет"},
        {"role": "assistant", "content": "Привет!"},
    ]
    assert speech.without_voice_refusals(history) == history[2:]


@pytest.mark.parametrize("role", ["owner", "adult", "kid", "guest"])
def test_every_role_knows_it_can_speak(role: str) -> None:
    assert "умеешь отвечать голосом" in llm.system_prompt(role, 11)


@pytest.mark.asyncio
async def test_round_video_is_heard_and_answered_with_voice(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, send, answer = harness
    _as_member(monkeypatch, _member())
    transcribe = _hearing(instance, "какая погода")
    message = _message("")
    message.text = None
    message.video_note = MagicMock(duration=20)
    await instance.on_message(message)
    transcribe.assert_awaited_once()
    instance._bot.download.assert_awaited_once_with(message.video_note)
    assert send.await_args_list[0].args[1] == "🗣 «какая погода»"
    assert answer.call_args.args[3] == "какая погода"
    instance._speak.assert_awaited_once_with("ответ")


@pytest.mark.asyncio
async def test_ordinary_video_is_declined_without_download(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, send, answer = harness
    _as_member(monkeypatch, _member())
    _hearing(instance, "слова")
    message = _message("")
    message.text = None
    message.video = MagicMock()
    await instance.on_message(message)
    instance._bot.download.assert_not_awaited()
    answer.assert_not_awaited()
    assert _sent(send) == bot.NO_VIDEO


@pytest.mark.asyncio
async def test_text_question_is_never_voiced(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, _, _ = harness
    _as_member(monkeypatch, _member())
    _hearing(instance, None)
    await instance.on_message(_message("вопрос"))
    instance._speak.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_voice_reply_leaves_the_text_answer(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, send, _ = harness
    _as_member(monkeypatch, _member())
    _hearing(instance, "какая погода")
    instance._bot.send_voice = AsyncMock(side_effect=TelegramAPIError(MagicMock(), "voice forbidden"))
    await instance.on_message(_voice_message())
    assert _sent(send) == "ответ"


@pytest.mark.parametrize(
    "reply",
    [
        "Смотри https://example.com",
        "```python\nprint(1)\n```",
        "Формула: $$x^2$$",
        "| a | b |\n|---|---|",
        "я" * (speech.MAX_SPOKEN_CHARS + 1),
        "🙂 ...",
    ],
)
def test_unreadable_replies_stay_text_only(reply: str) -> None:
    assert speech.speakable_text(reply) is None


def test_markup_is_not_read_aloud() -> None:
    assert speech.speakable_text("**Привет**, _мир_! 🙂") == "Привет , мир !"


@pytest.mark.parametrize(
    ("text", "language"),
    [
        ("Привет, как дела?", "ru"),
        ("How are you today?", "en"),
        ("Window sill.", "en"),
        ("Слово window значит окно", "ru"),
    ],
)
def test_voice_follows_the_language_of_the_reply(text: str, language: str) -> None:
    assert speech.reply_language(text) == language


def test_synthesised_speech_becomes_a_telegram_voice_note() -> None:
    pcm = array("h", (int(8000 * math.sin(i * 0.1)) for i in range(22_050))).tobytes()
    decoded = audio.decode_voice(audio.encode_voice(pcm, 22_050))
    assert abs(len(decoded) / audio.SAMPLE_BYTES - audio.SAMPLE_RATE) < 2000
    assert not audio.is_silent(decoded)


@pytest.mark.asyncio
async def test_voicing_failure_is_quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(speech, "_synthesize", MagicMock(side_effect=OSError("no model file")))
    assert await speech.speak_reply("привет") is None


@pytest.mark.asyncio
async def test_kid_voice_gets_the_same_safety_check_as_text(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, send, answer = harness
    _as_member(monkeypatch, _member("kid", 5))
    monkeypatch.setattr(access, "adult_chat_ids", AsyncMock(return_value=[9]))
    _hearing(instance, "меня бьют в школе")
    message = _voice_message()
    message.from_user.id = 5
    await instance.on_message(message)
    answer.assert_not_awaited()
    assert _sent(send) == bot.SAFETY_REPLY


@pytest.mark.asyncio
@pytest.mark.parametrize(("words", "reply"), [(None, bot.VOICE_FAILED), ("", bot.VOICE_NOT_HEARD)])
async def test_unusable_voice_is_explained_without_a_model_call(
    harness: Any, monkeypatch: pytest.MonkeyPatch, words: str | None, reply: str
) -> None:
    instance, send, answer = harness
    _as_member(monkeypatch, _member())
    _hearing(instance, words)
    await instance.on_message(_voice_message())
    answer.assert_not_awaited()
    assert _sent(send) == reply


@pytest.mark.asyncio
async def test_failed_voice_download_is_explained(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, send, _ = harness
    _as_member(monkeypatch, _member())
    transcribe = _hearing(instance, "слова")
    instance._bot.download = AsyncMock(side_effect=TimeoutError())
    await instance.on_message(_voice_message())
    transcribe.assert_not_awaited()
    assert _sent(send) == bot.VOICE_FAILED


@pytest.mark.asyncio
async def test_long_voice_is_refused_before_download(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, send, _ = harness
    _as_member(monkeypatch, _member())
    _hearing(instance, "слова")
    await instance.on_message(_voice_message(bot.MAX_VOICE_SECONDS + 1))
    instance._bot.download.assert_not_awaited()
    assert _sent(send) == bot.VOICE_TOO_LONG


@pytest.mark.asyncio
async def test_guest_out_of_budget_is_heard_by_the_free_model_only(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _, _ = harness
    guest = _member("guest")
    guest.money_limit_usd = Decimal("1")
    guest.spent_usd = Decimal("1")
    guest.spend_month = bot.LunaBot._today().replace(day=1)
    _as_member(monkeypatch, guest)
    transcribe = _hearing(instance, "слова")
    await instance.on_message(_voice_message())
    assert transcribe.await_args is not None
    assert transcribe.await_args.args[4] is True


@pytest.mark.asyncio
async def test_guest_voice_cost_is_recorded(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, _, _ = harness
    _as_member(monkeypatch, _member("guest"))
    record_spend = AsyncMock()
    monkeypatch.setattr(access, "record_spend", record_spend)

    async def costly(_role: str, _id: int, _audio: bytes, on_cost: Any, _local_only: bool) -> str:
        on_cost(0.25)
        return "слова"

    _hearing(instance, None)
    instance._transcribe = costly
    await instance.on_message(_voice_message())
    assert record_spend.await_args is not None
    assert record_spend.await_args.args[2] == 0.25


@pytest.mark.asyncio
async def test_voice_does_not_use_up_two_rate_slots(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, _, _ = harness
    _as_member(monkeypatch, _member())
    _hearing(instance, "слова")
    await instance.on_message(_voice_message())
    assert len(instance._request_times[1]) == 1


def _opus(amplitude: int, seconds: float = 0.5) -> bytes:
    """A real OGG/Opus clip, the format Telegram uses for voice notes."""
    buffer = io.BytesIO()
    with av.open(buffer, "w", format="ogg") as output:
        stream = output.add_stream("libopus", rate=48_000)
        frame_samples = 960
        for index in range(int(seconds * 48_000 / frame_samples)):
            pcm = array(
                "h", (int(amplitude * math.sin(index * frame_samples * 0.05 + i * 0.05)) for i in range(frame_samples))
            )
            frame = av.AudioFrame(format="s16", layout="mono", samples=frame_samples)
            frame.planes[0].update(pcm.tobytes())
            frame.rate = 48_000
            frame.pts = index * frame_samples
            output.mux(stream.encode(frame))
        output.mux(stream.encode(None))
    return buffer.getvalue()


def test_voice_note_decodes_to_speech_rate_wav() -> None:
    pcm = audio.decode_voice(_opus(8000))
    assert abs(len(pcm) / audio.SAMPLE_BYTES - audio.SAMPLE_RATE / 2) < 2000
    assert not audio.is_silent(pcm)
    with wave.open(io.BytesIO(audio.to_wav(pcm))) as result:
        assert (result.getframerate(), result.getnchannels()) == (audio.SAMPLE_RATE, 1)


def test_silent_voice_note_is_recognised() -> None:
    assert audio.is_silent(audio.decode_voice(_opus(0)))
    assert audio.is_silent(b"")


def test_broken_voice_note_is_a_value_error() -> None:
    with pytest.raises(ValueError, match="undecodable"):
        audio.decode_voice(b"not audio at all")


def _models_called(chat: AsyncMock) -> list[str]:
    return [call.args[1]["model"] for call in chat.await_args_list]


@pytest.mark.asyncio
async def test_voice_uses_only_the_free_model_when_it_hears(monkeypatch: pytest.MonkeyPatch) -> None:
    chat = AsyncMock(return_value={"content": " привет "})
    monkeypatch.setattr(llm, "_chat", chat)
    assert await llm.transcribe_voice("adult", 1, _opus(8000)) == "привет"
    assert _models_called(chat) == [llm.VOICE_LOCAL_MODEL]
    payload = chat.await_args_list[0].args[1]
    assert payload["disable_fallbacks"] is True
    assert "feature:stt" in payload["metadata"]["tags"]
    assert payload["messages"][0]["content"][0]["image_url"]["url"].startswith("data:audio/wav;base64,")


@pytest.mark.asyncio
async def test_silent_voice_calls_no_model(monkeypatch: pytest.MonkeyPatch) -> None:
    chat = AsyncMock()
    monkeypatch.setattr(llm, "_chat", chat)
    assert await llm.transcribe_voice("adult", 1, _opus(0)) == ""
    chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_cloud_model_hears_only_when_the_local_one_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    chat = AsyncMock(side_effect=[httpx.ConnectError("down"), {"content": "слова"}])
    monkeypatch.setattr(llm, "_chat", chat)
    assert await llm.transcribe_voice("adult", 1, _opus(8000)) == "слова"
    assert _models_called(chat) == [llm.VOICE_LOCAL_MODEL, llm.VOICE_CLOUD_MODEL]
    assert "input_audio" in chat.await_args_list[1].args[1]["messages"][0]["content"][0]


@pytest.mark.asyncio
async def test_cloud_model_double_checks_a_bare_timestamp(monkeypatch: pytest.MonkeyPatch) -> None:
    chat = AsyncMock(side_effect=[{"content": "00:00"}, {"content": ""}])
    monkeypatch.setattr(llm, "_chat", chat)
    assert await llm.transcribe_voice("adult", 1, _opus(8000)) == ""
    assert _models_called(chat) == [llm.VOICE_LOCAL_MODEL, llm.VOICE_CLOUD_MODEL]


@pytest.mark.asyncio
@pytest.mark.parametrize("echo", ["Транскрибируй следующий сегмент речи.", "Transcribe the following speech segment"])
async def test_repeated_instructions_are_not_taken_for_words(monkeypatch: pytest.MonkeyPatch, echo: str) -> None:
    monkeypatch.setattr(llm, "_chat", AsyncMock(return_value={"content": echo}))
    assert await llm.transcribe_voice("adult", 1, _opus(8000)) == ""


@pytest.mark.asyncio
async def test_local_only_voice_never_reaches_the_paid_model(monkeypatch: pytest.MonkeyPatch) -> None:
    chat = AsyncMock(side_effect=httpx.ConnectError("down"))
    monkeypatch.setattr(llm, "_chat", chat)
    assert await llm.transcribe_voice("guest", 1, _opus(8000), None, True) is None
    assert _models_called(chat) == [llm.VOICE_LOCAL_MODEL]


@pytest.mark.asyncio
async def test_transcribe_reports_failure_as_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm, "_chat", AsyncMock(side_effect=httpx.ConnectError("down")))
    assert await llm.transcribe_voice("adult", 1, _opus(8000)) is None
    assert await llm.transcribe_voice("adult", 1, b"not audio") is None


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


@pytest.mark.parametrize("role", ["owner", "adult", "kid", "guest"])
def test_every_role_is_answered_in_the_language_it_writes(role: str) -> None:
    assert "на языке последнего сообщения" in llm.system_prompt(role, 11)
    assert "по-русски, коротко" not in llm.system_prompt(role, 11)


def test_kids_use_the_same_models_as_adults_with_strict_search() -> None:
    assert llm.MODELS_BY_ROLE["kid"] == llm.MODELS_BY_ROLE["adult"]
    assert "kid" in llm.SEARCH_ROLES


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
        family=MagicMock(
            recall=AsyncMock(return_value=("Бабушка приезжает в мае",)), add=AsyncMock(return_value="saved")
        ),
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
    text, markup = await instance._memory_view(_member("owner"), 1)
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


def test_age_bands_and_their_borders() -> None:
    assert [llm.age_band(age) for age in (None, 5, 9, 10, 12, 13, 17)] == [
        "middle",
        "young",
        "young",
        "middle",
        "middle",
        "teen",
        "teen",
    ]


def test_teen_is_spoken_to_as_an_equal_under_the_same_safety_rules() -> None:
    teen, young = llm.system_prompt("kid", 15), llm.system_prompt("kid", 7)
    assert "на равных" in teen and "короткими фразами" not in teen
    assert "короткими фразами" in young and "на равных" not in young
    assert llm.KID_SAFETY in teen and llm.KID_SAFETY in young and llm.KID_SAFETY in llm.system_prompt("kid", None)


def _guest_ready(monkeypatch: pytest.MonkeyPatch, **values: Any) -> AssistantMember:
    member = AssistantMember(telegram_id=1, name="Гость", role="guest", **values)
    _as_member(monkeypatch, member)
    return member


@pytest.mark.asyncio
async def test_guest_has_no_daily_request_limit(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, _, answer = harness
    _guest_ready(monkeypatch)
    for number in range(25):
        instance._request_times[1].clear()
        await instance.on_message(_message(f"вопрос {number}"))
    assert answer.await_count == 25


@pytest.mark.asyncio
async def test_guest_whose_time_ended_is_told_and_costs_nothing(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, send, answer = harness
    _guest_ready(monkeypatch, access_until=datetime.now(UTC) - timedelta(minutes=1))
    await instance.on_message(_message("вопрос"))
    answer.assert_not_awaited()
    assert _sent(send) == bot.ACCESS_ENDED


@pytest.mark.asyncio
async def test_guest_over_money_limit_stays_on_the_free_model(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, _, answer = harness
    month = instance._today().replace(day=1)
    _guest_ready(monkeypatch, money_limit_usd=Decimal(1), spent_usd=Decimal("1.5"), spend_month=month)
    await instance.on_message(_message("вопрос"))
    assert answer.await_args.args[5].local_only is True


@pytest.mark.asyncio
async def test_guest_spending_is_recorded(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, _, answer = harness
    _guest_ready(monkeypatch)
    record: Any = AsyncMock()
    monkeypatch.setattr(access, "record_spend", record)

    async def priced(*args: Any) -> str:
        args[5].on_cost(0.25)
        args[5].on_cost(0.5)
        return "ответ"

    answer.side_effect = priced
    await instance.on_message(_message("вопрос"))
    assert record.await_args.args[1:3] == (1, 0.75)


@pytest.mark.asyncio
async def test_family_spending_is_not_metered_and_child_age_reaches_the_model(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _, answer = harness
    kid = _member("kid")
    kid.birth_year = instance._today().year - 14
    _as_member(monkeypatch, kid)
    await instance.on_message(_message("вопрос"))
    options = answer.await_args.args[5]
    assert options.age == 14 and options.on_cost is None and options.local_only is False


@pytest.mark.asyncio
async def test_local_only_never_reaches_a_paid_model(monkeypatch: pytest.MonkeyPatch) -> None:
    payloads: list[dict[str, Any]] = []

    async def fake_chat(client: Any, payload: dict[str, Any], on_cost: Any = None) -> dict[str, Any]:
        payloads.append(payload)
        raise httpx.ConnectError("local model is down")

    monkeypatch.setattr(llm, "_chat", fake_chat)
    await llm.generate_reply("guest", 1, [], "вопрос", options=llm.ReplyOptions(local_only=True))
    assert [p["model"] for p in payloads] == ["luna-local"] and payloads[0]["disable_fallbacks"] is True


@pytest.mark.asyncio
async def test_cost_comes_from_the_gateway_header(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = {"choices": [{"message": {"content": "ок"}}]}
        return httpx.Response(200, json=body, headers={"x-litellm-response-cost": "0.002"})

    costs: list[float] = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await llm._chat(client, {"model": "luna-fast"}, costs.append)
    assert costs == [0.002]


@pytest.mark.asyncio
async def test_quiz_level_follows_age(monkeypatch: pytest.MonkeyPatch) -> None:
    payloads: list[dict[str, Any]] = []

    async def fake_chat(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
        payloads.append(payload)
        return {"content": '{"q": "Вопрос?", "options": ["a", "b", "c", "d"], "correct": 0}'}

    monkeypatch.setattr(llm, "_chat", fake_chat)
    await llm.generate_quiz(1, 16)
    assert "старшеклассника" in payloads[0]["messages"][0]["content"]


@pytest.mark.parametrize(
    ("text", "fact"),
    [
        ("запомни для семьи, что у бабушки день рождения 5 мая", "у бабушки день рождения 5 мая"),
        ("Запомни для всей семьи: врач в среду", "врач в среду"),
        ("запиши для всех код домофона другой", "код домофона другой"),
        ("сохрани в общей памяти что кот боится пылесоса", "кот боится пылесоса"),
        ("запомни в семейной памяти\nпоездка в июле", "поездка в июле"),
    ],
)
def test_family_request_is_recognized(text: str, fact: str) -> None:
    assert bot.family_request(text) == fact


@pytest.mark.parametrize(
    "text",
    [
        "запомни, что я люблю футбол",
        "запомни для меня номер",
        "что семья думает",
        "запомни для семьи",
        "для семьи запомни",
    ],
)
def test_ordinary_messages_are_not_family_requests(text: str) -> None:
    assert bot.family_request(text) is None


@pytest.mark.asyncio
async def test_family_request_is_saved_without_the_model(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, memory = _with_memory(harness, monkeypatch, "kid")
    _, send, answer = harness
    await instance.on_message(_message("запомни для семьи, что кот боится пылесоса"))
    memory.family.add.assert_awaited_once_with(1, "Тест", "кот боится пылесоса")
    answer.assert_not_awaited()
    memory.remember.assert_not_called()
    assert _sent(send) == bot.FAMILY_REPLIES["saved"]


@pytest.mark.asyncio
async def test_guest_cannot_write_to_family_memory(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, memory = _with_memory(harness, monkeypatch, "guest")
    _, _, answer = harness
    await instance.on_message(_message("запомни для семьи, что кот боится пылесоса"))
    memory.family.add.assert_not_awaited()
    answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_child_in_danger_is_not_filed_as_a_note(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, memory = _with_memory(harness, monkeypatch, "kid")
    alert = AsyncMock()
    monkeypatch.setattr(instance, "_alert_parents", alert)
    await instance.on_message(_message("запомни для семьи, что меня бьют"))
    alert.assert_awaited_once()
    memory.family.add.assert_not_awaited()


def _reply_to(text: str, prompt: str) -> Any:
    message = _message(text)
    message.reply_to_message = MagicMock(text=prompt, from_user=MagicMock(is_bot=True))
    return message


@pytest.mark.asyncio
async def test_answer_to_the_family_prompt_is_kept_word_for_word(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, memory = _with_memory(harness, monkeypatch, "adult")
    await instance.on_message(_reply_to("дача в августе", bot.FAMILY_PROMPT))
    memory.family.add.assert_awaited_once_with(1, "Тест", "дача в августе")


@pytest.mark.asyncio
async def test_guest_answering_the_family_prompt_writes_nothing(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, memory = _with_memory(harness, monkeypatch, "guest")
    await instance.on_message(_reply_to("дача в августе", bot.FAMILY_PROMPT))
    memory.family.add.assert_not_awaited()


@pytest.mark.asyncio
async def test_family_notes_reach_the_reply_by_role(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, memory = _with_memory(harness, monkeypatch, "kid")
    _, _, answer = harness
    await instance.on_message(_message("когда приедет бабушка?"))
    memory.family.recall.assert_awaited_once_with("kid", "когда приедет бабушка?")
    assert answer.await_args.args[5].family_facts == ("Бабушка приезжает в мае",)


def test_family_memory_buttons_are_closed_to_guests() -> None:
    for prefix in ("fam", "fama", "famd", "famy"):
        assert not bot.LunaBot._callback_allowed("guest", prefix)
        assert bot.LunaBot._callback_allowed("kid", prefix) and bot.LunaBot._callback_allowed("owner", prefix)


def _labels(markup: Any) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


@pytest.mark.asyncio
async def test_memory_view_offers_family_memory_only_to_family(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, _ = _with_memory(harness, monkeypatch, "kid")
    _, markup = await instance._memory_view(_member("kid"), 0)
    assert "🏠 Общая память" in _labels(markup)
    _, markup = await instance._memory_view(_member("guest"), 0)
    assert markup is None


def _family_note(author: int, index: int = 1) -> Any:
    return MagicMock(id=uuid.UUID(int=index), content="заметка", author_id=author, author_name="Аня")


@pytest.mark.asyncio
async def test_child_may_delete_only_own_family_notes(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, memory = _with_memory(harness, monkeypatch, "kid")
    memory.family.list_facts = AsyncMock(return_value=[_family_note(1, 1), _family_note(2, 2)])
    _, markup = await instance._family_view(_member("kid"), 0)
    assert [label for label in _labels(markup) if label.startswith("🗑")] == ["🗑 1"]
    _, markup = await instance._family_view(_member("adult"), 0)
    assert [label for label in _labels(markup) if label.startswith("🗑")] == ["🗑 1", "🗑 2"]


@pytest.mark.asyncio
async def test_old_button_cannot_delete_someone_elses_note(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, memory = _with_memory(harness, monkeypatch, "kid")
    note = _family_note(2)
    memory.family.get = AsyncMock(return_value=note)
    memory.family.forget = AsyncMock()
    memory.family.list_facts = AsyncMock(return_value=[note])
    callback = MagicMock(message=MagicMock(spec=Message, edit_text=AsyncMock()))
    await instance._forget_family_fact(callback, _member("kid"), note.id.hex)
    memory.family.forget.assert_not_awaited()
    await instance._forget_family_fact(callback, _member("adult"), note.id.hex)
    memory.family.forget.assert_awaited_once_with(note.id)
