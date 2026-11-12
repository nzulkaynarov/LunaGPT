"""Luna is a cat while the model thinks: playful phrases in a Telegram draft, gone when the answer lands.

The first phrase fits what Luna is doing (a photo, a sum, a voice note), a web search shows what she looks up,
the time of day and holidays add their own, approved family phrases join in, and now and then a rare one
appears."""

import asyncio
import itertools
import logging
import random
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager, suppress
from datetime import date, datetime

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

logger = logging.getLogger("lunagpt.assistant.waiting")

FIRST_PHRASE_AFTER_SECONDS = 3.0  # a quick answer needs no apology
NEXT_PHRASE_AFTER_SECONDS = 8.0  # a draft lives 30 seconds, so it never runs out
MAX_DRAFT_ID = 2**31 - 1
RARE_CHANCE = 1 / 50
MAX_QUERY_SHOWN = 60
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
KIND_PHRASES = {
    "photo": (
        "🔍 Разглядываю фото, обнюхиваю уголки…",
        "🖼 Смотрю на фото, сощурив глаза…",
        "🐾 Трогаю фото лапкой, вдруг там мышь…",
    ),
    "math": (
        "✏️ Считаю на лапках… пальцев не хватает",
        "🧮 Перекладываю косточки на счётах…",
        "🐾 Проверяю ответ хвостом, он никогда не врёт…",
    ),
    "voice": (
        "👂 Навострила уши…",
        "🎙 Прочищаю горлышко, сейчас скажу…",
        "🐱 Слушаю внимательно, даже усы замерли…",
    ),
}
MORNING_PHRASES = (
    "☕ Ещё не проснулась, но уже думаю…",
    "🌅 Потягиваюсь и думаю одновременно…",
)
NIGHT_PHRASES = (
    "🌙 Ночью коты думают лучше…",
    "🦉 Ночная смена, думаю при свете луны…",
)
HOLIDAY_PHRASES = {
    "new_year": ("🎄 Запуталась в гирлянде, сейчас выберусь…", "❄️ Ловлю снежинку с ответом…"),
    "navruz": ("🌷 Помешиваю сумаляк, думаю…", "🌱 Ответ прорастает, как весенняя травка…"),
}
RARE_PHRASES = (
    "🦄 Позвонила единорогу, он знает всё…",
    "🚀 Слетала в космос за ответом, уже возвращаюсь…",
    "👑 Королева котов думает над твоим вопросом…",
    "🐉 Разбудила дракона, он тоже думает…",
    "🎩 Достаю ответ из шляпы фокусника…",
    "🛸 Котики с другой планеты прислали подсказку…",
    "🐈 Мой прапрапрадедушка был котом учёного. Вспоминаю, что он рассказывал…",
    "🍀 Нашла четырёхлистный клевер, ответ будет удачным…",
)
RARE_MARK = "✨ Редкая фраза!\n"


def holiday(day: date) -> str | None:
    if (day.month == 12 and day.day >= 25) or (day.month == 1 and day.day <= 7):
        return "new_year"
    if day.month == 3 and 18 <= day.day <= 24:
        return "navruz"
    return None


def moment_phrases(now: datetime) -> tuple[str, ...]:
    """Phrases for the time of day and a holiday, mixed into the usual ones."""
    found: tuple[str, ...] = ()
    if 5 <= now.hour < 10:
        found += MORNING_PHRASES
    elif now.hour >= 22 or now.hour < 5:
        found += NIGHT_PHRASES
    occasion = holiday(now.date())
    if occasion is not None:
        found += HOLIDAY_PHRASES[occasion]
    return found


def search_phrase(query: str) -> str:
    shown = " ".join(query.split())
    if len(shown) > MAX_QUERY_SHOWN:
        shown = shown[: MAX_QUERY_SHOWN - 1].rstrip() + "…"
    return f"🐾 Ищу лапкой: «{shown}»…"


class Waiting:
    """What the cat shows: the phrase pool for this answer and a search Luna has just started."""

    def __init__(
        self,
        kind: str = "chat",
        now: datetime | None = None,
        family: Sequence[str] = (),
        rng: random.Random | None = None,
    ) -> None:
        self._rng = rng or random.Random()
        self._first = list(KIND_PHRASES.get(kind, ()))
        pool = list(PHRASES) + list(moment_phrases(now or datetime.now())) + list(family)
        self._rng.shuffle(self._first)
        self._rest = itertools.cycle(self._rng.sample(pool, len(pool)))
        self._searches: list[str] = []
        self.changed = asyncio.Event()

    def searching(self, query: str) -> None:
        """Called from the model loop when it searches; the phrase shows right away."""
        self._searches.append(search_phrase(query))
        self.changed.set()

    def next_phrase(self) -> str:
        if self._searches:
            return self._searches.pop(0)
        if self._first:
            return self._first.pop()
        if self._rng.random() < RARE_CHANCE:
            return RARE_MARK + self._rng.choice(RARE_PHRASES)
        return next(self._rest)


async def _pause(waiting: Waiting, seconds: float) -> None:
    """Sleep, but wake at once when a search starts."""
    with suppress(TimeoutError):
        await asyncio.wait_for(waiting.changed.wait(), seconds)
    waiting.changed.clear()


async def _show_phrases(bot: Bot, chat_id: int, waiting: Waiting) -> None:
    draft_id = random.randint(1, MAX_DRAFT_ID)
    await _pause(waiting, FIRST_PHRASE_AFTER_SECONDS)
    while True:
        try:
            await bot.send_message_draft(chat_id, draft_id, text=waiting.next_phrase())
        except TelegramAPIError as exc:
            logger.warning("could not show a waiting phrase: %s", type(exc).__name__)  # the typing status remains
            return
        await _pause(waiting, NEXT_PHRASE_AFTER_SECONDS)


@asynccontextmanager
async def cat_is_thinking(bot: Bot, chat_id: int, waiting: Waiting | None = None) -> AsyncIterator[Waiting]:
    """Stopped before the caller sends the answer, so no late phrase can follow it."""
    waiting = waiting or Waiting()
    task = asyncio.create_task(_show_phrases(bot, chat_id, waiting))
    try:
        yield waiting
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
