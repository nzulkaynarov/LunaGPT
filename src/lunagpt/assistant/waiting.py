"""Luna is a cat while the model thinks: playful phrases in a Telegram draft, gone when the answer lands."""

import asyncio
import itertools
import logging
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

logger = logging.getLogger("lunagpt.assistant.waiting")

FIRST_PHRASE_AFTER_SECONDS = 3.0  # a quick answer needs no apology
NEXT_PHRASE_AFTER_SECONDS = 8.0  # a draft lives 30 seconds, so it never runs out
MAX_DRAFT_ID = 2**31 - 1
PHRASES = (
    "🐾 Подожди, у меня же лапки…",
    "😺 Готовлю ответ, мяу…",
    "🐈 Всё ещё думаю и ищу ответ возле миски…",
    "🐾 Листаю интернет одной лапкой…",
    "😼 Точу когти об этот вопрос…",
    "🐱 Спросила у соседского кота, ждём…",
    "🧶 Распутываю клубок мыслей…",
    "🐟 Обменяла рыбку на пару фактов…",
    "😸 Мурчу над ответом…",
    "🐾 Печатаю одной лапкой, второй держу мысль…",
    "🐈‍⬛ Смотрю в стену. Это тоже процесс…",
    "😽 Почти готово, только умоюсь…",
    "🐾 Гоняю мысль по коридору, сейчас поймаю…",
    "📦 Думаю в коробке, там лучше думается…",
    "🐱 Сбросила вопрос со стола, смотрю, что будет…",
    "😾 Ответ спрятался под диваном, достаю…",
)


async def _show_phrases(bot: Bot, chat_id: int) -> None:
    draft_id = random.randint(1, MAX_DRAFT_ID)
    await asyncio.sleep(FIRST_PHRASE_AFTER_SECONDS)
    for phrase in itertools.cycle(random.sample(PHRASES, len(PHRASES))):
        try:
            await bot.send_message_draft(chat_id, draft_id, text=phrase)
        except TelegramAPIError as exc:
            logger.warning("could not show a waiting phrase: %s", type(exc).__name__)  # the typing status remains
            return
        await asyncio.sleep(NEXT_PHRASE_AFTER_SECONDS)


@asynccontextmanager
async def cat_is_thinking(bot: Bot, chat_id: int) -> AsyncIterator[None]:
    """Stopped before the caller sends the answer, so no late phrase can follow it."""
    task = asyncio.create_task(_show_phrases(bot, chat_id))
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
