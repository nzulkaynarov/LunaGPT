"""Luna's voice: short replies spoken by a local model, free and without leaving the PC."""

import asyncio
import functools
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from piper import PiperVoice

from lunagpt.assistant.audio import SAMPLE_BYTES, encode_voice

logger = logging.getLogger("lunagpt.assistant.speech")

VOICES_DIR = Path("/app/voices")
VOICE_FILES = {"ru": "ru_RU-irina-medium.onnx", "en": "en_US-lessac-medium.onnx"}
MAX_SPOKEN_CHARS = 700  # about a minute of speech; anything longer is better read
# Links, code, tables and formulas make no sense read aloud, so those replies stay text only.
NOT_SPEAKABLE = re.compile(r"https?://|www\.|```|\$\$|\\\(|\\\[|^\s*\|", re.MULTILINE)
UNSPOKEN_CHARS = re.compile(r"_|[^\w\s.,!?:;'\"«»()—–-]")
CYRILLIC = re.compile(r"[А-Яа-яЁё]")
LATIN = re.compile(r"[A-Za-z]")


@dataclass(frozen=True)
class SpokenReply:
    """A Telegram voice note: OGG/Opus bytes and whole seconds."""

    ogg: bytes
    seconds: int


def speakable_text(reply: str) -> str | None:
    """Return the reply cleaned for reading aloud, or None if it should stay text only."""
    if len(reply) > MAX_SPOKEN_CHARS or NOT_SPEAKABLE.search(reply):
        return None
    cleaned = " ".join(UNSPOKEN_CHARS.sub(" ", reply).split())
    return cleaned if any(ch.isalpha() for ch in cleaned) else None


def reply_language(text: str) -> str:
    return "ru" if len(CYRILLIC.findall(text)) >= len(LATIN.findall(text)) else "en"


@functools.cache
def _voice(language: str) -> PiperVoice:
    return PiperVoice.load(VOICES_DIR / VOICE_FILES[language])


def _synthesize(text: str) -> SpokenReply:
    voice = _voice(reply_language(text))
    pcm = b"".join(chunk.audio_int16_bytes for chunk in voice.synthesize(text))
    rate = voice.config.sample_rate
    return SpokenReply(encode_voice(pcm, rate), max(1, round(len(pcm) / SAMPLE_BYTES / rate)))


async def speak_reply(reply: str) -> SpokenReply | None:
    """Voice a reply, or return None: the text has already been sent, so a failure here must stay quiet."""
    text = speakable_text(reply)
    if text is None:
        return None
    try:
        return await asyncio.to_thread(_synthesize, text)
    except Exception:
        logger.warning("could not voice a reply", exc_info=True)
        return None
