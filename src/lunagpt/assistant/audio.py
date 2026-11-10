"""Prepare Telegram voice notes for the models: the local one cannot read OGG/Opus."""

import io
import math
import wave
from array import array

import av

SAMPLE_RATE = 16_000
SAMPLE_BYTES = 2
SILENCE_RMS = 100  # about -50 dBFS, well below a voice on a phone microphone


def decode_voice(ogg: bytes) -> bytes:
    """Return mono 16 kHz 16-bit PCM, or raise ValueError for audio that cannot be decoded."""
    resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
    pcm = bytearray()
    try:
        with av.open(io.BytesIO(ogg), mode="r") as container:
            for frame in container.decode(audio=0):
                for chunk in resampler.resample(frame):
                    pcm += bytes(chunk.planes[0])[: chunk.samples * SAMPLE_BYTES]
        for chunk in resampler.resample(None):
            pcm += bytes(chunk.planes[0])[: chunk.samples * SAMPLE_BYTES]
    except (av.FFmpegError, IndexError) as exc:
        raise ValueError("undecodable voice note") from exc
    return bytes(pcm)


def is_silent(pcm: bytes) -> bool:
    """True for a recording with no sound worth sending to a model."""
    samples = array("h", pcm)
    return not samples or math.sqrt(sum(s * s for s in samples) / len(samples)) < SILENCE_RMS


def to_wav(pcm: bytes) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(SAMPLE_BYTES)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(pcm)
    return buffer.getvalue()
