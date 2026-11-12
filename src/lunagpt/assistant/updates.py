"""After an update Luna tells each person herself, with a cat joke and a button that brings the new menu.

A Telegram reply keyboard only changes when a message carries it, so without this people kept pressing
/start. The version is derived from what a person can see: their role's menu and the newest "Что нового"
lines. Notices go out in the daytime only, so nobody is woken at night."""

import hashlib
import random
import re
from datetime import UTC, datetime, timedelta

from lunagpt.assistant import about
from lunagpt.config import settings

UPDATE_PREFIX = "upd"
DAY_STARTS = 9
DAY_ENDS = 21
MAX_NEWS_LINES = 4
PHRASES = (
    "😺 Мяу, я, кажется, поумнела! Перезапусти меня.",
    "🐾 Я поспала в коробке и проснулась умнее. Нажми, чтобы перезапустить меня.",
    "😸 У меня новые лапки… то есть новые умения. Перезапусти меня!",
    "🐈 Я немного обновилась, пока ты не смотрел. Перезапусти меня, и покажу, что нового.",
    "🧶 Распутала клубок и нашла там новые функции. Перезапусти меня!",
)
RESTARTED = "🐾 Готово, я перезапустилась! Меню внизу обновлено."
NEWS_LINE = re.compile(r"^- (\d{1,2} \S+ \d{4}): (.+)$", re.MULTILINE)


def newest_news() -> list[str]:
    """The "Что нового" lines with the newest date: what a person gets to hear about."""
    text = "\n\n".join(body for name, body in about._sections() if name == "all")
    found = NEWS_LINE.findall(text)
    if not found:
        return []
    newest = found[0][0]
    return [line for date, line in found if date == newest][:MAX_NEWS_LINES]


def version(role: str, layout: list[list[str]]) -> str:
    """Changes when this role's menu or the newest news change."""
    seen = repr(layout) + ("\n".join(newest_news()) if role != "guest" else "")
    return hashlib.sha256(seen.encode()).hexdigest()[:16]


def notice(role: str, rng: random.Random | None = None) -> str:
    phrase = (rng or random.Random()).choice(PHRASES)
    news = newest_news() if role != "guest" else []  # the news name family features
    return phrase + ("\n\nЧто нового:\n" + "\n".join(f"• {line}" for line in news) if news else "")


def is_daytime(now: datetime | None = None) -> bool:
    local = (now or datetime.now(UTC)) + timedelta(hours=settings.luna_utc_offset_hours)
    return DAY_STARTS <= local.hour < DAY_ENDS
