"""Luna's voice: replies spoken by Gemini through the gateway.

The owner chose Gemini over the local Piper voices on 2026-11-13 ("локальная ужасная"); the lighter Gemini
voice model stands in when the main one fails, and with neither the text reply stays on its own."""

import io
import logging
import re
import uuid
import wave
from dataclasses import dataclass

import httpx

from lunagpt.assistant.audio import SAMPLE_BYTES, encode_voice
from lunagpt.config import settings

logger = logging.getLogger("lunagpt.assistant.speech")

VOICE_MODELS = ("luna-voice", "luna-voice-lite")
VOICE_NAME = "Capella"  # the owner's choice on 2026-11-13; it reads Russian and English
SPEECH_TIMEOUT_SECONDS = 90.0
MAX_SPOKEN_CHARS = 3000  # about three and a half minutes, enough for a bedtime story
# Links, code, tables and formulas make no sense read aloud, so those replies stay text only.
NOT_SPEAKABLE = re.compile(r"https?://|www\.|```|\$\$|\\\(|\\\[|^\s*\|", re.MULTILINE)
UNSPOKEN_CHARS = re.compile(r"_|[^\w\s.,!?:;'\"«»()—–-]")
VOICE_REQUEST = re.compile(
    r"голосом|озвуч\w*|вслух|out loud|aloud|by voice|with your voice|voice message", re.IGNORECASE
)
BARE_REQUEST_WORDS = frozenset(
    [
        "ответь",
        "скажи",
        "повтори",
        "произнеси",
        "прочитай",
        "прочти",
        "расскажи",
        "сделай",
        "можешь",
        "можно",
        "пожалуйста",
        "это",
        "этот",
        "эту",
        "его",
        "её",
        "текст",
        "сообщение",
        "ответ",
        "мне",
        "ещё",
        "раз",
        "а",
        "теперь",
        "say",
        "speak",
        "read",
        "answer",
        "repeat",
        "tell",
        "me",
        "it",
        "that",
        "this",
        "please",
        "again",
        "now",
        "can",
        "you",
    ]
)
VOICE_REFUSAL = re.compile(
    r"не могу[^.!?]*(голос|говорить)|текстов\w+ (ии|ассистент|формат)|can(no|')t speak", re.IGNORECASE
)
CYRILLIC = re.compile(r"[А-Яа-яЁё]")
LATIN = re.compile(r"[A-Za-z]")


@dataclass(frozen=True)
class SpokenReply:
    """A Telegram voice note: OGG/Opus bytes and whole seconds."""

    ogg: bytes
    seconds: int


def asks_for_voice(text: str) -> bool:
    """True when a typed message asks for the answer to be spoken."""
    return VOICE_REQUEST.search(text) is not None


def without_voice_words(text: str) -> str:
    """Remove the request to speak: on seeing it the local model claims it cannot and refuses the question."""
    return " ".join(VOICE_REQUEST.sub(" ", text).split()).strip(" ,.") or text


def is_bare_voice_request(text: str) -> bool:
    """True for "ответь голосом" with no question of its own: the last answer should be said aloud."""
    rest = VOICE_REQUEST.sub(" ", text.lower())
    return asks_for_voice(text) and set(re.findall(r"\w+", rest)) <= BARE_REQUEST_WORDS


def without_voice_refusals(history: list[dict[str, str]]) -> list[dict[str, str]]:
    """Drop past turns where the model denied it can speak, because it copies them word for word."""
    kept: list[dict[str, str]] = []
    for turn in history:
        if turn["role"] == "assistant" and VOICE_REFUSAL.search(turn["content"]):
            if kept and kept[-1]["role"] == "user":
                kept.pop()
            continue
        kept.append(turn)
    return kept


def speakable_text(reply: str) -> str | None:
    """Return the reply cleaned for reading aloud, or None if it should stay text only."""
    if len(reply) > MAX_SPOKEN_CHARS or NOT_SPEAKABLE.search(reply):
        return None
    cleaned = " ".join(UNSPOKEN_CHARS.sub(" ", reply).split())
    return cleaned if any(ch.isalpha() for ch in cleaned) else None


def reply_language(text: str) -> str:
    return "ru" if len(CYRILLIC.findall(text)) >= len(LATIN.findall(text)) else "en"


def speech_endpoint() -> str:
    chat: str = settings.litellm_endpoint
    return chat.replace("/chat/completions", "/audio/speech")


def from_wav(data: bytes) -> SpokenReply:
    """Gemini answers with 16-bit mono WAV; Telegram wants OGG/Opus.

    Gemini's own WAV carries a C2PA provenance chunk after the sound, and the gateway wraps that whole file
    as if it were raw sound: the inner header clicked at the start and the C2PA bytes hissed at the end.
    So a WAV found inside the sound is read as a file, which keeps only its data chunk."""
    with wave.open(io.BytesIO(data)) as source:
        if source.getsampwidth() != SAMPLE_BYTES or source.getnchannels() != 1:
            raise ValueError("unexpected audio format")
        rate = source.getframerate()
        pcm = source.readframes(source.getnframes())
    if pcm[:4] == b"RIFF" and pcm[8:12] == b"WAVE":
        return from_wav(pcm)
    if not pcm:
        raise ValueError("empty audio")
    return SpokenReply(encode_voice(pcm, rate), max(1, round(len(pcm) / SAMPLE_BYTES / rate)))


async def _synthesize(text: str) -> SpokenReply:
    async with httpx.AsyncClient() as client:
        for model in VOICE_MODELS:
            try:
                response = await client.post(
                    speech_endpoint(),
                    json={
                        "model": model,
                        "input": text,
                        "voice": VOICE_NAME,
                        "metadata": {
                            "trace_id": str(uuid.uuid4()),
                            "source": "luna_assistant",
                            "tags": ["client:luna-assistant", "feature:voice-reply", f"lang:{reply_language(text)}"],
                        },
                    },
                    headers={"Authorization": f"Bearer {settings.litellm_assistant_key}"},
                    timeout=SPEECH_TIMEOUT_SECONDS,
                )
                response.raise_for_status()
                return from_wav(response.content)
            except (httpx.HTTPError, ValueError, wave.Error, EOFError) as exc:
                logger.warning("voice model %s failed: %s", model, type(exc).__name__)
    raise RuntimeError("no voice model answered")


async def speak_reply(reply: str) -> SpokenReply | None:
    """Voice a reply, or return None: the text has already been sent, so a failure here must stay quiet."""
    text = speakable_text(reply)
    if text is None:
        return None
    try:
        return await _synthesize(text)
    except Exception:
        logger.warning("could not voice a reply", exc_info=True)
        return None
