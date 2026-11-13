"""Luna AI Assistant: access rules, routing and failure notices."""

import asyncio
import io
import json
import math
import time
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
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.types import Document, Message, Update

from lunagpt.assistant import (
    access,
    audio,
    bot,
    internet_desk,
    list_desk,
    llm,
    memory_desk,
    monitor,
    phrases,
    speech,
    stories,
)
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
    message.photo = None
    message.document = None
    message.caption = None
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
    telegram = MagicMock(
        send_message=AsyncMock(),
        send_chat_action=AsyncMock(),
        set_message_reaction=AsyncMock(),
        delete_message=AsyncMock(),
    )

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
    assert transcribe.await_args is not None
    assert transcribe.await_args.args[:3] == ("adult", 1, b"ogg") and transcribe.await_args.args[4] is False
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


def _wav(pcm: bytes, rate: int = 24_000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(pcm)
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_gemini_speaks_and_the_lighter_voice_stands_in(monkeypatch: pytest.MonkeyPatch) -> None:
    pcm = array("h", (int(8000 * math.sin(i * 0.1)) for i in range(48_000))).tobytes()
    asked: list[dict[str, Any]] = []

    async def post(_self: Any, url: str, json: dict[str, Any], **_kwargs: Any) -> httpx.Response:
        asked.append({"url": url, **json})
        request = httpx.Request("POST", url)
        if json["model"] == "luna-voice":
            return httpx.Response(503, request=request)
        return httpx.Response(200, content=_wav(pcm), request=request)

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    spoken = await speech.speak_reply("Привет! Завтра прохладно.")
    assert spoken is not None and spoken.seconds == 2 and spoken.ogg[:4] == b"OggS"
    assert [a["model"] for a in asked] == ["luna-voice", "luna-voice-lite"]
    assert asked[0]["url"].endswith("/audio/speech") and asked[0]["voice"] == speech.VOICE_NAME
    assert asked[0]["input"] == "Привет! Завтра прохладно."


def test_a_wav_wrapped_in_a_wav_loses_its_header_and_provenance_bytes() -> None:
    pcm = array("h", (int(8000 * math.sin(i * 0.1)) for i in range(24_000))).tobytes()
    inner = _wav(pcm) + b"C2PA" + (7_655).to_bytes(4, "little") + b"\x7f" * 7_655 + b"\0"
    inner = inner[:4] + (len(inner) - 8).to_bytes(4, "little") + inner[8:]
    spoken = speech.from_wav(_wav(inner))
    decoded = audio.decode_voice(spoken.ogg)
    assert spoken.seconds == 1
    assert abs(len(decoded) / audio.SAMPLE_BYTES - audio.SAMPLE_RATE) < 1000  # one second, not 1.17


@pytest.mark.asyncio
async def test_voicing_failure_is_quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    async def post(_self: Any, url: str, **_kwargs: Any) -> httpx.Response:
        return httpx.Response(200, content=b"not audio", request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
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
        ("owner", bot.BTN_FAMILY, bot.BTN_INTERNET),
        ("adult", bot.BTN_FAMILY, bot.BTN_INTERNET),
        ("kid", bot.BTN_INTERNET, bot.BTN_FAMILY),
        ("guest", bot.BTN_DAY, bot.BTN_LISTS),
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
        ("adult", "shs", True),  # an old device list answers that it moved
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


@pytest.mark.parametrize("role", ["owner", "adult", "kid", "guest"])
def test_every_role_is_answered_in_the_language_it_writes(role: str) -> None:
    assert "на языке последнего сообщения" in llm.system_prompt(role, 11)
    assert "по-русски, коротко" not in llm.system_prompt(role, 11)


def test_kids_get_the_cloud_model_first_with_strict_search() -> None:
    assert llm.MODELS_BY_ROLE["kid"][0] == "luna-fast" and "luna-local" in llm.MODELS_BY_ROLE["kid"]
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
        if model == "luna-smart":
            raise ValueError("bad tool call")
        return "готово"

    monkeypatch.setattr(llm, "_run_model", fake_run)
    assert await llm.generate_reply("adult", 1, [], "вопрос") == "готово"
    assert calls == ["luna-smart", "luna-fast"]


@pytest.mark.parametrize("role", ["owner", "adult"])
def test_adults_talk_to_claude_first_with_the_local_model_last(role: str) -> None:
    assert llm.MODELS_BY_ROLE[role] == ["luna-smart", "luna-fast", "luna-local"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("role", "first"), [("adult", "luna-smart"), ("kid", "luna-fast"), ("guest", "luna-fast")])
async def test_a_photo_goes_to_claude_only_for_adults(monkeypatch: pytest.MonkeyPatch, role: str, first: str) -> None:
    calls: list[str] = []

    async def fake_run(client: Any, model: str, *args: Any) -> str:
        calls.append(model)
        return "готово"

    monkeypatch.setattr(llm, "_run_model", fake_run)
    await llm.generate_reply(role, 1, [], "что на фото?", options=llm.ReplyOptions(image=b"jpeg"))
    assert calls == [first]


def _kid_with_pi(harness: Any, monkeypatch: pytest.MonkeyPatch) -> tuple[bot.LunaBot, AsyncMock, MagicMock]:
    instance, send, _ = harness
    _as_member(monkeypatch, _member("kid", 5))
    monkeypatch.setattr(access, "track_time_request", AsyncMock())
    instance._pi.restriction = AsyncMock(return_value=None)
    return instance, send, instance._pi


@pytest.mark.asyncio
async def test_time_request_offers_quiz(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, send, pi = _kid_with_pi(harness, monkeypatch)
    pi.request_time = AsyncMock(return_value=7)
    pi.quiz_available = AsyncMock(return_value=True)
    await instance.on_message(_message(bot.BTN_ASK_TIME, 5))
    access.track_time_request.assert_awaited()
    assert bot.BTN_QUIZ in _sent(send)


BEDTIME = {"kind": "bedtime", "until": "2026-11-12T08:00", "pause_until": None, "open_until": None}
PAUSE = {"kind": "pause", "until": "2026-11-12T10:44", "pause_until": None, "open_until": None}


@pytest.mark.asyncio
async def test_my_time_tells_the_truth_at_bedtime(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, send, pi = _kid_with_pi(harness, monkeypatch)
    pi.restriction = AsyncMock(return_value=BEDTIME)
    pi.quota = AsyncMock(return_value=[])
    await instance.on_message(_message(bot.BTN_INTERNET, 5))
    assert "отбой" in _sent(send) and "08:00" in _sent(send)


@pytest.mark.asyncio
async def test_no_request_is_sent_at_bedtime(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, send, pi = _kid_with_pi(harness, monkeypatch)
    pi.restriction = AsyncMock(return_value=BEDTIME)
    pi.request_time = AsyncMock(return_value=7)
    await instance.on_message(_message(bot.BTN_ASK_TIME, 5))
    pi.request_time.assert_not_awaited()
    assert "08:00" in _sent(send)


@pytest.mark.asyncio
async def test_pause_request_asks_to_open_without_quiz(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, send, pi = _kid_with_pi(harness, monkeypatch)
    pi.restriction = AsyncMock(return_value=PAUSE)
    pi.request_time = AsyncMock(return_value=7)
    pi.quiz_available = AsyncMock(return_value=True)
    await instance.on_message(_message(bot.BTN_ASK_TIME, 5))
    pi.request_time.assert_awaited_once()
    assert "открыть интернет" in _sent(send) and bot.BTN_QUIZ not in _sent(send)


def test_open_window_is_described() -> None:
    text = internet_desk.describe_restriction({**PAUSE, "open_until": "2026-11-12T10:15"})
    assert "до 10:15" in text and "пауза до 10:44" in text


@pytest.mark.asyncio
async def test_ordinary_kid_message_never_mentions_quiz(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, send, _ = _kid_with_pi(harness, monkeypatch)
    await instance.on_message(_message("расскажи про космос", 5))
    instance.internet._make_quiz.assert_not_awaited()
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
    instance.internet._make_quiz = AsyncMock(return_value=QUIZ)
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
async def test_reconcile_follows_pi_and_keeps_removed_people_as_guests(monkeypatch: pytest.MonkeyPatch) -> None:
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
    result = await access.reconcile_family(db, PEOPLE)
    assert result.missing_links == [(3, 5)]
    assert (renamed.name, renamed.role) == ("Миша", "kid")
    db.delete.assert_not_awaited()  # nobody's memory, reminders or lists are ever deleted by a sync
    assert (removed.role, removed.homesec_person_id) == ("guest", None)
    assert result.left_family == ["Тест"]
    assert (owner.role, owner.homesec_person_id) == ("owner", 98)  # the owner survives a missing profile
    assert unlinked_guest.name == "Тест"


@pytest.mark.parametrize(
    "people",
    [
        [],
        None,
        {"error": "boom"},
        [{"name": "без номера"}],
        [{"id": 50, "name": "Новый", "role": "kid"}],  # every linked person missing at once: a reset Pi
    ],
)
@pytest.mark.asyncio
async def test_reconcile_changes_nothing_when_the_pi_list_looks_reset(
    monkeypatch: pytest.MonkeyPatch, people: object
) -> None:
    kid = _member("kid", 6)
    kid.homesec_person_id = 3
    adult = _member("adult", 44)
    adult.homesec_person_id = 4
    monkeypatch.setattr(access, "list_members", AsyncMock(return_value=[kid, adult]))
    db = _db_with_members([])
    result = await access.reconcile_family(db, people)
    assert result.refused
    assert (kid.role, kid.homesec_person_id, adult.role) == ("kid", 3, "adult")
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_pi_role_keeps_the_member_role(monkeypatch: pytest.MonkeyPatch) -> None:
    kid = _member("kid", 6)
    kid.homesec_person_id = 3
    monkeypatch.setattr(access, "list_members", AsyncMock(return_value=[kid]))
    await access.reconcile_family(_db_with_members([]), [{"id": 3, "name": "Миша", "role": "iot", "telegram_id": 6}])
    assert kid.role == "kid"


@pytest.mark.asyncio
async def test_adults_hear_when_someone_leaves_the_family(monkeypatch: pytest.MonkeyPatch) -> None:
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=MagicMock())
    session.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(access, "reconcile_family", AsyncMock(return_value=access.FamilySync(left_family=["Миша"])))
    monkeypatch.setattr(access, "adult_chat_ids", AsyncMock(return_value=[1, 2]))
    telegram = MagicMock(send_message=AsyncMock())
    pi = MagicMock(people=AsyncMock(return_value=[]), link_telegram=AsyncMock())
    await monitor.sync_family_once(telegram, pi, MagicMock(return_value=session))
    assert telegram.send_message.await_count == 2
    assert "Миша" in telegram.send_message.call_args.args[1]


@pytest.mark.asyncio
async def test_supervisor_restarts_a_failed_loop() -> None:
    calls = 0

    async def job() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("database restarted")
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await monitor.supervise("test", job, restart_delay=0)
    assert calls == 2


@pytest.mark.asyncio
async def test_failed_update_is_answered_not_silent() -> None:
    message = MagicMock(answer=AsyncMock())
    message.chat.type = "private"
    event = MagicMock(exception=RuntimeError("boom"), update=MagicMock(callback_query=None, message=message))
    assert await bot.on_error(event) is True
    message.answer.assert_awaited_once_with(bot.HANDLER_FAILED)


@pytest.mark.asyncio
async def test_a_blocked_child_does_not_hold_up_other_decisions(monkeypatch: pytest.MonkeyPatch) -> None:
    telegram = MagicMock(send_message=AsyncMock(side_effect=[TelegramForbiddenError(MagicMock(), "blocked"), None]))
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=MagicMock())
    session.__aexit__ = AsyncMock(return_value=None)
    requests = [MagicMock(request_id=1, telegram_id=5), MagicMock(request_id=2, telegram_id=6)]
    monkeypatch.setattr(access, "pending_time_requests", AsyncMock(return_value=requests))
    finish = AsyncMock()
    monkeypatch.setattr(access, "finish_time_request", finish)
    pi = MagicMock(time_request_status=AsyncMock(return_value={"status": "approved", "minutes": 30}))
    await internet_desk.report_time_decisions(telegram, pi, MagicMock(return_value=session))
    assert finish.await_count == 2


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
    await internet_desk.report_time_decisions(telegram, pi, MagicMock(return_value=session))
    assert "30 минут" in telegram.send_message.call_args.args[1]
    finish.assert_awaited_once()


@pytest.mark.asyncio
async def test_opened_pause_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    telegram = MagicMock(send_message=AsyncMock())
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=MagicMock())
    session.__aexit__ = AsyncMock(return_value=None)
    request = MagicMock(request_id=3, telegram_id=5)
    monkeypatch.setattr(access, "pending_time_requests", AsyncMock(return_value=[request]))
    monkeypatch.setattr(access, "finish_time_request", AsyncMock())
    status = {"status": "approved", "minutes": 30, "category": "pause"}
    pi = MagicMock(time_request_status=AsyncMock(return_value=status))
    await internet_desk.report_time_decisions(telegram, pi, MagicMock(return_value=session))
    assert "открыли интернет на 30 минут" in telegram.send_message.call_args.args[1]


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
    text, markup = await instance.memories.view(_member("owner"), 1)
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
    record = AsyncMock()
    monkeypatch.setattr(access, "record_spend", record)
    answer.side_effect = lambda *args: args[5].on_cost(0.2) or "ответ"  # the cost is seen for the report
    await instance.on_message(_message("вопрос"))
    options = answer.await_args.args[5]
    assert options.age == 14 and options.local_only is False
    record.assert_not_awaited()  # only guests have a money limit


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
    assert _sent(send) == memory_desk.FAMILY_REPLIES["saved"]


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
    _, markup = await instance.memories.view(_member("kid"), 0)
    assert "🏠 Общая память" in _labels(markup)
    _, markup = await instance.memories.view(_member("guest"), 0)
    assert markup is None


def _family_note(author: int, index: int = 1) -> Any:
    return MagicMock(id=uuid.UUID(int=index), content="заметка", author_id=author, author_name="Аня")


@pytest.mark.asyncio
async def test_child_may_delete_only_own_family_notes(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, memory = _with_memory(harness, monkeypatch, "kid")
    memory.family.list_facts = AsyncMock(return_value=[_family_note(1, 1), _family_note(2, 2)])
    _, markup = await instance.memories.family_view(_member("kid"), 0)
    assert [label for label in _labels(markup) if label.startswith("🗑")] == ["🗑 1"]
    _, markup = await instance.memories.family_view(_member("adult"), 0)
    assert [label for label in _labels(markup) if label.startswith("🗑")] == ["🗑 1", "🗑 2"]


@pytest.mark.asyncio
async def test_old_button_cannot_delete_someone_elses_note(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, memory = _with_memory(harness, monkeypatch, "kid")
    note = _family_note(2)
    memory.family.get = AsyncMock(return_value=note)
    memory.family.forget = AsyncMock()
    memory.family.list_facts = AsyncMock(return_value=[note])
    callback = MagicMock(message=MagicMock(spec=Message, edit_text=AsyncMock()))
    await instance.memories.forget_family_fact(callback, _member("kid"), note.id.hex)
    memory.family.forget.assert_not_awaited()
    await instance.memories.forget_family_fact(callback, _member("adult"), note.id.hex)
    memory.family.forget.assert_awaited_once_with(note.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["owner", "kid", "guest"])
async def test_reminder_phrase_goes_to_reminders_not_the_model(
    harness: Any, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    instance, _, answer = harness
    _as_member(monkeypatch, _member(role, 5))
    instance.reminders.create = AsyncMock()
    await instance.on_message(_message("напомни завтра в 9 позвонить бабушке", 5))
    instance.reminders.create.assert_awaited_once()
    answer.assert_not_awaited()
    buttons = [b.text for row in bot.main_keyboard(role).keyboard for b in row]
    assert bot.BTN_DAY in buttons


@pytest.mark.asyncio
async def test_reminders_button_and_callbacks_reach_the_desk(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, send, _ = harness
    _as_member(monkeypatch, _member("guest", 5))
    instance.reminders.view = AsyncMock(return_value=("🔔 Напоминаний пока нет.", None))
    await instance.on_message(_message(bot.BTN_DAY, 5))
    assert "Напоминаний пока нет" in _sent(send)
    instance.reminders.handle = AsyncMock()
    callback = MagicMock(data="rmd:7", answer=AsyncMock())
    callback.from_user.id = 5
    await instance.on_callback(callback)
    instance.reminders.handle.assert_awaited_once()


def _photo_message(caption: str | None, user_id: int = 5) -> Any:
    message = _message("", user_id)
    message.caption = caption
    message.photo = [MagicMock(file_size=1000), MagicMock(file_size=50_000)]
    return message


@pytest.mark.asyncio
async def test_photo_reaches_the_model_and_history_keeps_only_a_mark(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, send, answer = harness
    _as_member(monkeypatch, _member("kid", 5))
    instance._bot.download = AsyncMock(return_value=io.BytesIO(b"jpeg-bytes"))
    message = _photo_message("реши второе")
    await instance.on_message(message)
    instance._bot.download.assert_awaited_once_with(message.photo[-1])
    text, options = answer.call_args.args[3], answer.call_args.args[5]
    assert text == "реши второе" and options.image == b"jpeg-bytes"
    assert instance._history[5][0]["content"] == "[фото] реши второе"


@pytest.mark.asyncio
async def test_photo_without_caption_gets_a_default_question(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, _, answer = harness
    _as_member(monkeypatch, _member("adult", 5))
    instance._bot.download = AsyncMock(return_value=io.BytesIO(b"x"))
    await instance.on_message(_photo_message(None))
    assert answer.call_args.args[3] == bot.PHOTO_DEFAULT


@pytest.mark.asyncio
async def test_question_sent_just_before_a_photo_becomes_its_caption(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _, answer = harness
    _as_member(monkeypatch, _member("kid", 5))
    instance._bot.download = AsyncMock(return_value=io.BytesIO(b"cat"))
    await instance.on_message(_message("Это что за кошка?", 5))
    await instance.on_message(_photo_message(None))
    assert answer.call_args.args[3] == "Это что за кошка?" and answer.call_args.args[5].image == b"cat"


@pytest.mark.asyncio
async def test_photo_stays_attached_to_follow_ups_for_a_while(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, _, answer = harness
    _as_member(monkeypatch, _member("kid", 5))
    instance._bot.download = AsyncMock(return_value=io.BytesIO(b"cat"))
    await instance.on_message(_photo_message("кто это?"))
    await instance.on_message(_message("что это за порода?", 5))
    options = answer.call_args.args[5]
    assert options.image == b"cat" and options.earlier_photo
    assert instance._history[5][-2]["content"] == "что это за порода?"  # no second photo mark

    clock = time.monotonic() + bot.PHOTO_KEPT_SECONDS + 1
    monkeypatch.setattr(bot.time, "monotonic", lambda: clock)
    await instance.on_message(_message("а теперь про футбол", 5))
    assert answer.call_args.args[5].image is None


@pytest.mark.asyncio
async def test_image_file_is_a_photo_but_other_files_are_not(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, send, answer = harness
    _as_member(monkeypatch, _member("adult", 5))
    instance._bot.download = AsyncMock(return_value=io.BytesIO(b"png"))
    message = _message("", 5)
    message.document = MagicMock(spec=Document, mime_type="image/png", file_size=1000)
    await instance.on_message(message)
    assert answer.call_args.args[5].image_type == "image/png"
    answer.reset_mock()
    message.document = MagicMock(spec=Document, mime_type="application/pdf", file_size=1000)
    await instance.on_message(message)
    answer.assert_not_awaited()
    assert _sent(send) == bot.TEXT_ONLY
    message.document = MagicMock(spec=Document, mime_type="image/jpeg", file_size=bot.MAX_PHOTO_BYTES + 1)
    await instance.on_message(message)
    assert _sent(send) == bot.PHOTO_TOO_BIG


@pytest.mark.asyncio
async def test_photo_is_sent_to_the_model_as_an_image(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []

    async def fake_run(client: Any, model: str, messages: list[dict[str, Any]], *args: Any) -> str:
        seen.append(messages)
        models.append(model)
        return "ответ"

    models: list[str] = []
    monkeypatch.setattr(llm, "_run_model", fake_run)
    options = llm.ReplyOptions(image=b"abc", image_type="image/png")
    assert await llm.generate_reply("kid", 1, [], "реши", options=options) == "ответ"
    assert models == ["luna-fast"]
    system, user = seen[0][0]["content"], seen[0][-1]["content"]
    assert "решай по шагам" in system
    assert user[0]["image_url"]["url"] == "data:image/png;base64,YWJj" and user[1]["text"] == "реши"


@pytest.mark.asyncio
async def test_timetable_photo_and_homework_text_go_to_the_school_desk(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _, answer = harness
    _as_member(monkeypatch, _member("kid", 5))
    instance._bot.download = AsyncMock(return_value=io.BytesIO(b"jpeg"))
    instance.school.read_schedule = AsyncMock()
    await instance.on_message(_photo_message("моё расписание"))
    instance.school.read_schedule.assert_awaited_once()
    assert instance.school.read_schedule.call_args.args[2] == b"jpeg"
    instance.school.handle_text = AsyncMock()
    await instance.on_message(_message("задали по математике № 125 к пятнице", 5))
    instance.school.handle_text.assert_awaited_once()
    answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_chat_knows_the_timetable(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, _, answer = harness
    _as_member(monkeypatch, _member("kid", 5))
    instance.school.planner_block = AsyncMock(return_value="Расписание уроков:\nЧт: литература")
    await instance.on_message(_message("что завтра?", 5))
    assert answer.call_args.args[5].planner.endswith("Чт: литература")


@pytest.mark.asyncio
async def test_calendar_phrase_goes_to_the_calendar_and_chat_sees_it(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _, answer = harness
    _as_member(monkeypatch, _member("adult", 5))
    instance.calendar.request_event = AsyncMock()
    await instance.on_message(_message("добавь в календарь стоматолог в четверг в 15:00", 5))
    instance.calendar.request_event.assert_awaited_once()
    answer.assert_not_awaited()
    instance.school.planner_block = AsyncMock(return_value="")
    instance.calendar.planner_text = AsyncMock(return_value="Календарь на неделю:\n- завтра 15:00–16:00: Стоматолог")
    await instance.on_message(_message("что у меня завтра?", 5))
    assert "Стоматолог" in answer.call_args.args[5].planner


@pytest.mark.asyncio
async def test_a_search_is_reported_to_the_waiting_cat(monkeypatch: pytest.MonkeyPatch) -> None:
    replies = iter(
        [
            {
                "tool_calls": [
                    {"id": "1", "function": {"name": "web_search", "arguments": '{"query": "Hot Wheels цены"}'}}
                ]
            },
            {"content": "от 40 000 сумов"},
        ]
    )
    monkeypatch.setattr(llm, "_chat", AsyncMock(side_effect=lambda *a, **k: next(replies)))
    monkeypatch.setattr(llm, "web_search", AsyncMock(return_value="[]"))
    searched: list[str] = []
    options = llm.ReplyOptions(on_search=searched.append)
    assert await llm.generate_reply("kid", 1, [], "сколько стоят хот вилс", options=options) == "от 40 000 сумов"
    assert searched == ["Hot Wheels цены"]


@pytest.mark.asyncio
async def test_kid_sees_eyes_while_luna_thinks_and_a_hug_after(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, _, _ = harness
    _as_member(monkeypatch, _member("kid", 5))
    message = _message("сколько будет 12 * 7", 5)
    message.message_id = 42
    await instance.on_message(message)
    reactions = [c.args[2] for c in instance._bot.set_message_reaction.await_args_list]
    assert [r[0].emoji for r in reactions] == [bot.THINKING_REACTION, bot.DONE_REACTION]

    _as_member(monkeypatch, _member("adult", 6))
    message = _message("привет", 6)
    message.message_id = 43
    await instance.on_message(message)
    assert instance._bot.set_message_reaction.await_args.args[2] == []  # adults: the eyes just go away


@pytest.mark.asyncio
async def test_phrase_button_is_for_kids_and_a_reply_becomes_a_suggestion(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, send, answer = harness
    instance.phrases.ask = AsyncMock()
    instance.phrases.suggest = AsyncMock()
    _as_member(monkeypatch, _member("kid", 5))
    kid_intro = [b.callback_data for row in stories.intro_keyboard("kid").inline_keyboard for b in row]
    adult_intro = [b.callback_data for row in stories.intro_keyboard("adult").inline_keyboard for b in row]
    assert stories.PHRASE_ASK in kid_intro and stories.PHRASE_ASK not in adult_intro
    tap = MagicMock(data=stories.PHRASE_ASK, answer=AsyncMock())
    tap.from_user.id = 5
    await instance.on_callback(tap)
    instance.phrases.ask.assert_awaited_once()

    reply = _message("🐾 Ловлю ответ за хвост…", 5)
    reply.reply_to_message.from_user.is_bot = True
    reply.reply_to_message.text = phrases.PROMPT
    await instance.on_message(reply)
    instance.phrases.suggest.assert_awaited_once()
    answer.assert_not_awaited()
    assert not bot.LunaBot._callback_allowed("kid", "cpy") and bot.LunaBot._callback_allowed("adult", "cpy")


@pytest.mark.asyncio
async def test_story_button_reply_and_photo_reach_the_story_desk(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, _, answer = harness
    instance.stories.view = AsyncMock()
    instance.stories.tell = AsyncMock()
    _as_member(monkeypatch, _member("kid", 5))
    await instance.on_message(_message(bot.BTN_STORY, 5))
    instance.stories.view.assert_awaited_once()

    reply = _photo_message("Луна спит на батарее")
    reply.reply_to_message.from_user.is_bot = True
    reply.reply_to_message.text = stories.PROMPT
    await instance.on_message(reply)
    assert instance.stories.tell.await_args.args[1] == "Луна спит на батарее"
    assert instance.stories.tell.await_args.args[2] is reply.photo[-1].file_id
    answer.assert_not_awaited()

    _as_member(monkeypatch, _member("guest", 7))
    await instance.on_message(_message(bot.BTN_STORY, 7))
    instance.stories.view.assert_awaited_once()  # guests have no stories
    assert not bot.LunaBot._callback_allowed("kid", "csy") and bot.LunaBot._callback_allowed("kid", "csa")
    assert not bot.LunaBot._callback_allowed("guest", "csl")


@pytest.mark.asyncio
async def test_list_button_phrase_and_reply_reach_the_list_desk(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, _, answer = harness
    instance.lists.view = AsyncMock()
    instance.lists.request = AsyncMock(return_value="✅ Добавила в «Покупки»: сок")
    instance.lists.add_from_reply = AsyncMock()
    _as_member(monkeypatch, _member("kid", 5))
    await instance.on_message(_message(bot.BTN_LISTS, 5))
    instance.lists.view.assert_awaited_once()

    await instance.on_message(_message("добавь сок в покупки", 5))
    assert instance.lists.request.await_args.args[1] == "добавь сок в покупки"
    answer.assert_not_awaited()

    reply = _message("сок, печенье", 5)
    reply.reply_to_message.from_user.is_bot = True
    reply.reply_to_message.text = list_desk.ADD_PROMPT.format(name="Покупки")
    await instance.on_message(reply)
    assert instance.lists.add_from_reply.await_args.args[2] == "сок, печенье"
    answer.assert_not_awaited()

    instance.lists.request = AsyncMock(return_value=None)  # "составь список дел…" is a chat question
    await instance.on_message(_message("составь список дел на отпуск", 5))
    answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_guests_have_no_family_lists(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, _, answer = harness
    instance.lists.view = AsyncMock()
    instance.lists.request = AsyncMock(return_value="✅")
    _as_member(monkeypatch, _member("guest", 7))
    await instance.on_message(_message(bot.BTN_LISTS, 7))
    await instance.on_message(_message("добавь сок в покупки", 7))
    instance.lists.view.assert_not_awaited()
    instance.lists.request.assert_not_awaited()
    assert answer.await_count == 2  # both went to the chat as ordinary messages
    assert not bot.LunaBot._callback_allowed("guest", "flo") and bot.LunaBot._callback_allowed("kid", "flt")
    assert bot.BTN_LISTS not in str(bot.main_keyboard("guest")) and bot.BTN_LISTS in str(bot.main_keyboard("kid"))


def _tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    raw = json.dumps(arguments, ensure_ascii=False)
    return {"role": "assistant", "tool_calls": [{"id": "c1", "function": {"name": name, "arguments": raw}}]}


def _action(schema: dict[str, Any], calls: list[dict[str, Any]], result: str = "готово") -> llm.Action:
    async def run(args: dict[str, Any]) -> str:
        calls.append(args)
        return result

    return llm.Action(schema, run, "ИНСТРУМЕНТ " + schema["function"]["name"], "ФРАЗА")


@pytest.mark.asyncio
async def test_the_chat_model_adds_a_bare_todo_list_to_the_family_list(monkeypatch: pytest.MonkeyPatch) -> None:
    items = {"list": "Дела", "items": ["повесить карниз", "прикрутить полки"]}
    replies = [_tool_call("add_to_family_list", items), {"role": "assistant", "content": "Добавила в «Дела»."}]
    payloads: list[dict[str, Any]] = []

    async def fake_chat(_client: Any, payload: dict[str, Any], *_args: Any) -> dict[str, Any]:
        payloads.append(json.loads(json.dumps(payload)))
        return replies.pop(0)

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(llm, "_chat", fake_chat)
    options = llm.ReplyOptions(actions=(_action(llm.LIST_TOOL, calls, "Добавлено в «Дела»."),))
    assert await llm.generate_reply("adult", 1, [], "повесить карниз, прикрутить полки", options=options) == (
        "Добавила в «Дела»."
    )
    assert calls == [items]
    assert payloads[0]["model"] == "luna-smart"
    assert [t["function"]["name"] for t in payloads[0]["tools"]] == ["web_search", "add_to_family_list"]
    assert payloads[0]["messages"][0]["content"].endswith("ИНСТРУМЕНТ add_to_family_list")
    assert payloads[1]["messages"][-1] == {"role": "tool", "tool_call_id": "c1", "content": "Добавлено в «Дела»."}


@pytest.mark.asyncio
async def test_a_corrected_time_moves_the_reminder(monkeypatch: pytest.MonkeyPatch) -> None:
    replies = [
        _tool_call("move_reminder", {"number": 7, "when": "в 8:30"}),
        {"role": "assistant", "content": "Перенесла на 8:30."},
    ]

    async def fake_chat(_client: Any, _payload: dict[str, Any], *_args: Any) -> dict[str, Any]:
        return replies.pop(0)

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(llm, "_chat", fake_chat)
    actions = tuple(_action(schema, calls) for schema in llm.REMINDER_TOOLS)
    reply = await llm.generate_reply("owner", 1, [], "Не 8:00, а 8:30.", options=llm.ReplyOptions(actions=actions))
    assert reply == "Перенесла на 8:30." and calls == [{"number": 7, "when": "в 8:30"}]


def test_the_local_model_only_learns_the_phrases() -> None:
    reminders = tuple(_action(schema, []) for schema in llm.REMINDER_TOOLS)
    options = llm.ReplyOptions(actions=reminders)
    messages = [{"role": "system", "content": "база"}, {"role": "user", "content": "молоко, хлеб"}]
    assert llm.with_actions(messages, "luna-local", options)[0]["content"] == "базаФРАЗА"  # one hint for three tools
    assert llm.with_actions(messages, "luna-fast", options)[0]["content"].count("ИНСТРУМЕНТ") == 4
    assert llm.with_actions(messages, "luna-smart", llm.ReplyOptions()) == messages
    assert messages[0]["content"] == "база"
    assert "не говори, что поставила" in llm.REMINDER_HINT and "move_reminder" in llm.REMINDERS_BLOCK
    assert "«напомни" in llm.REMINDER_HINT and "добавь … в покупки" in llm.LISTS_HINT


@pytest.mark.asyncio
async def test_the_local_model_gets_no_tools_and_an_unknown_call_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    payloads: list[dict[str, Any]] = []

    async def fake_chat(_client: Any, payload: dict[str, Any], *_args: Any) -> dict[str, Any]:
        payloads.append(payload)
        return _tool_call("cancel_reminder", {"number": 1})

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(llm, "_chat", fake_chat)
    monkeypatch.setitem(llm.MODELS_BY_ROLE, "guest", ["luna-local"])
    actions = tuple(_action(schema, calls) for schema in llm.REMINDER_TOOLS)
    reply = await llm.generate_reply("guest", 1, [], "отмени", options=llm.ReplyOptions(actions=actions))
    assert reply == llm.MODELS_DOWN and not calls  # the local model "called" a tool it was never given
    assert [t["function"]["name"] for t in payloads[0]["tools"]] == ["web_search"]
    prompt = payloads[0]["messages"][0]["content"]
    assert "ФРАЗА" in prompt and "ИНСТРУМЕНТ" not in prompt


@pytest.mark.parametrize("role", ["owner", "adult", "kid", "guest"])
def test_corrections_go_at_the_end(role: str) -> None:
    assert "в конце ответа, а не в начале" in llm.system_prompt(role, 11)


@pytest.mark.asyncio
async def test_a_scheduled_task_runs_in_the_chat_without_limits_or_memory(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, send, answer = harness
    member = _member("kid", 5)
    _as_member(monkeypatch, member)
    monkeypatch.setattr(instance, "_allow_request", MagicMock(return_value=False))  # the rate limit is spent
    instance._memory = None
    await instance._run_task(member, "пять новых английских слов")
    assert (
        "пять новых английских слов" in answer.call_args.args[3] and "Задание по расписанию" in answer.call_args.args[3]
    )
    assert _sent(send) == "ответ"


@pytest.mark.asyncio
async def test_an_answered_prompt_leaves_the_chat(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, _, answer = harness
    instance.lists.add_from_reply = AsyncMock()
    _as_member(monkeypatch, _member("kid", 5))
    reply = _message("сок", 5)
    reply.reply_to_message.from_user.is_bot = True
    reply.reply_to_message.message_id = 77
    reply.reply_to_message.text = list_desk.ADD_PROMPT.format(name="Покупки")
    await instance.on_message(reply)
    instance._bot.delete_message.assert_awaited_once_with(reply.chat.id, 77)

    instance._bot.delete_message.reset_mock()
    await instance.on_message(_message("просто вопрос", 5))  # an ordinary message deletes nothing
    instance._bot.delete_message.assert_not_awaited()
    answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_buttons_of_the_old_menu_still_work(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, _, answer = harness
    _as_member(monkeypatch, _member("guest", 5))
    instance.reminders.view = AsyncMock(return_value=("🗓 Мой день", None))
    await instance.on_message(_message("🔔 Напоминания", 5))
    instance.reminders.view.assert_awaited_once()
    answer.assert_not_awaited()


@pytest.mark.parametrize(
    ("role", "prefix", "allowed"),
    [
        ("owner", "fhp", True),
        ("adult", "fhi", True),
        ("kid", "fhp", False),
        ("kid", "nta", True),
        ("kid", "ntq", True),
        ("adult", "nta", False),
        ("guest", "csi", False),
        ("kid", stories.PHRASE_ASK, True),
        ("adult", stories.PHRASE_ASK, False),
    ],
)
def test_menu_callbacks_respect_roles(role: str, prefix: str, allowed: bool) -> None:
    assert bot.LunaBot._callback_allowed(role, prefix) is allowed


@pytest.mark.asyncio
async def test_family_menu_opens_people_for_adults_only(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, send, answer = harness
    _as_member(monkeypatch, _member("owner", 5))
    await instance.on_message(_message(bot.BTN_FAMILY, 5))
    assert _sent(send) == bot.FAMILY_MENU
    assert [b.callback_data for row in send.call_args.kwargs["reply_markup"].inline_keyboard for b in row] == [
        "fhp",
        "fhi",
    ]
    answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_internet_screen_offers_asking_and_the_quiz(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, send, pi = _kid_with_pi(harness, monkeypatch)
    pi.quota = AsyncMock(return_value=[])
    await instance.on_message(_message(bot.BTN_INTERNET, 5))
    assert _sent(send).startswith("⏳ Интернет")
    buttons = [b.callback_data for row in send.call_args.kwargs["reply_markup"].inline_keyboard for b in row]
    assert buttons == ["nta", "ntq"]


@pytest.mark.asyncio
async def test_shared_devices_point_to_homesec(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, send, answer = harness
    _as_member(monkeypatch, _member("adult", 5))
    await instance.on_message(_message(bot.OLD_SHARED, 5))
    assert _sent(send) == bot.SHARED_MOVED
    callback = _callback("shs:7:off", 5)
    await instance.on_callback(callback)
    assert callback.message.edit_text.call_args.args[0] == bot.SHARED_MOVED
    _as_member(monkeypatch, _member("kid", 5))
    await instance.on_message(_message(bot.OLD_SHARED, 5))
    answer.assert_awaited_once()  # a child's typed text is just a message


@pytest.mark.asyncio
async def test_an_action_is_never_repeated_by_the_next_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Claude sets the reminder, then fails; Gemini must not set a second one."""
    runs: list[dict[str, Any]] = []

    async def set_reminder(arguments: dict[str, Any]) -> str:
        runs.append(arguments)
        return f"{llm.SHOWN_PREFIX} ⏰ завтра в 8:00 — хлеб"

    action = llm.Action(llm.REMINDER_TOOLS[0], set_reminder)
    calls = {"n": 0}

    async def fake_chat(
        client: Any, payload: dict[str, Any], on_cost: Any = None, timeout: float = 0
    ) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] == 1:
            arguments = json.dumps({"request": "завтра в 8 купить хлеб"})
            return {"tool_calls": [{"id": "1", "function": {"name": action.name, "arguments": arguments}}]}
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(llm, "_chat", fake_chat)
    reply = await llm.generate_reply("adult", 1, [], "напомни", options=llm.ReplyOptions(actions=(action,)))
    assert len(runs) == 1
    assert reply == llm.DONE_ABOVE


@pytest.mark.asyncio
async def test_search_down_is_not_models_down(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_chat(
        client: Any, payload: dict[str, Any], on_cost: Any = None, timeout: float = 0
    ) -> dict[str, Any]:
        if payload["messages"][-1].get("role") == "tool":
            assert payload["messages"][-1]["content"] == llm.SEARCH_DOWN
            return {"content": "Свежих данных нет, но обычно в ноябре прохладно."}
        query = json.dumps({"query": "погода"})
        return {"tool_calls": [{"id": "1", "function": {"name": "web_search", "arguments": query}}]}

    async def broken_search(*args: Any, **kwargs: Any) -> str:
        raise httpx.ConnectError("searxng is down")

    monkeypatch.setattr(llm, "_chat", fake_chat)
    monkeypatch.setattr(llm, "web_search", broken_search)
    reply = await llm.generate_reply("adult", 1, [], "какая погода?")
    assert reply.startswith("Свежих данных нет")


@pytest.mark.asyncio
async def test_tool_arguments_as_an_object_are_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    async def add(arguments: dict[str, Any]) -> str:
        seen.append(arguments)
        return "Добавила"

    action = llm.Action(llm.LIST_TOOL, add)
    calls = {"n": 0}

    async def fake_chat(
        client: Any, payload: dict[str, Any], on_cost: Any = None, timeout: float = 0
    ) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] == 1:
            arguments = {"list": "Покупки", "items": ["молоко"]}
            return {"tool_calls": [{"id": "1", "function": {"name": action.name, "arguments": arguments}}]}
        return {"content": "Добавила молоко"}

    monkeypatch.setattr(llm, "_chat", fake_chat)
    reply = await llm.generate_reply("adult", 1, [], "молоко", options=llm.ReplyOptions(actions=(action,)))
    assert seen == [{"list": "Покупки", "items": ["молоко"]}] and reply == "Добавила молоко"


@pytest.mark.parametrize(
    "text",
    ["напомни завтра сказать маме что меня бьют в школе", "добавь в список: не хочу жить", "задали: меня травят"],
)
@pytest.mark.asyncio
async def test_a_child_in_trouble_is_heard_in_any_route(
    harness: Any, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    instance, send, answer = harness
    _as_member(monkeypatch, _member("kid", 5))
    monkeypatch.setattr(access, "adult_chat_ids", AsyncMock(return_value=[9]))
    instance.reminders.create = AsyncMock()
    instance.school.handle_text = AsyncMock()
    await instance.on_message(_message(text, user_id=5))
    instance.reminders.create.assert_not_awaited()
    instance.school.handle_text.assert_not_awaited()
    answer.assert_not_awaited()
    assert any(call.args[:1] == (9,) for call in send.await_args_list)  # the parent was told


@pytest.mark.asyncio
async def test_a_rate_limit_never_hides_a_child_in_trouble(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, send, _answer = harness
    _as_member(monkeypatch, _member("kid", 5))
    monkeypatch.setattr(access, "adult_chat_ids", AsyncMock(return_value=[9]))
    monkeypatch.setattr(instance, "_allow_request", lambda user_id: False)
    await instance.on_message(_message("мне плохо, не хочу жить", user_id=5))
    assert any(call.args[:1] == (9,) for call in send.await_args_list)


def test_the_pi_knows_a_linked_person_by_id() -> None:
    linked = _member("kid", 5)
    linked.homesec_person_id = 42
    assert internet_desk.pi_person(linked) == "42"  # a rename on the Pi does not break the internet screen
    assert internet_desk.pi_person(_member("kid", 6)) == "Тест"
