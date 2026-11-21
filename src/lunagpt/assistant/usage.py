"""What each person uses, counted per local day without any text, and the owner's weekly report.

The product counts as working when people come back by themselves several days a week; these
counts are how the owner sees that, and which features nobody opens.
"""

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lunagpt.assistant.calendar_desk import CALENDAR_PREFIXES
from lunagpt.assistant.list_desk import LIST_PREFIXES
from lunagpt.assistant.people import PEOPLE_PREFIXES
from lunagpt.assistant.phrases import PHRASE_PREFIXES
from lunagpt.assistant.reminder_desk import REMINDER_PREFIXES
from lunagpt.assistant.school_desk import SCHOOL_PREFIXES
from lunagpt.assistant.stories import STORY_PREFIXES
from lunagpt.assistant.telegram import Rich, esc, rich_table, send_long
from lunagpt.models import AssistantMember, AssistantUsage

logger = logging.getLogger("lunagpt.assistant.usage")

FEATURE_NAMES = {
    "chat": "чат",
    "voice": "голос",
    "photo": "фото",
    "reminders": "напоминания",
    "brief": "утренняя сводка",
    "day": "мой день",
    "lists": "списки",
    "memory": "память",
    "school": "уроки и домашка",
    "calendar": "календарь",
    "internet": "интернет",
    "quiz": "викторина",
    "stories": "истории",
    "phrases": "фразы",
    "people": "люди",
    "family": "семья",
    "help": "помощь",
}
# Features worth naming when nobody opened them all week.
OPTIONAL_FEATURES = ("lists", "memory", "reminders", "brief", "school", "stories", "phrases", "quiz")
REPORT_WEEKDAY = 6  # Sunday
REPORT_HOUR = 20
REPORT_CHECK_SECONDS = 600
WEEK_DAYS = 7

_PREFIX_GROUPS: list[tuple[frozenset[str] | set[str], str]] = [
    (frozenset({"rb", "rbt", "rbx", "rbc"}), "brief"),
    (REMINDER_PREFIXES, "reminders"),
    (SCHOOL_PREFIXES, "school"),
    (CALENDAR_PREFIXES, "calendar"),
    (LIST_PREFIXES, "lists"),
    (STORY_PREFIXES, "stories"),
    (PHRASE_PREFIXES, "phrases"),
    (PEOPLE_PREFIXES | {"fhp", "fhi"}, "people"),
    (frozenset({"mem", "memd", "memc", "memy", "fam", "fama", "famd", "famy"}), "memory"),
    (frozenset({"quiz", "ntq"}), "quiz"),
    (frozenset({"nta"}), "internet"),
]


def feature_of_prefix(prefix: str) -> str | None:
    """The feature a button press belongs to; None for service buttons such as the update notice."""
    for prefixes, feature in _PREFIX_GROUPS:
        if prefix in prefixes:
            return feature
    return None


async def record(
    sessions: async_sessionmaker[AsyncSession], member_id: int, feature: str, today: date, cost: float = 0.0
) -> None:
    """Count one use; a failure is logged and never reaches the person."""
    spent = Decimal(str(cost))
    statement = (
        insert(AssistantUsage)
        .values(member_id=member_id, day=today, feature=feature, count=1, cost_usd=spent)
        .on_conflict_do_update(
            index_elements=[AssistantUsage.member_id, AssistantUsage.day, AssistantUsage.feature],
            set_={"count": AssistantUsage.count + 1, "cost_usd": AssistantUsage.cost_usd + spent},
        )
    )
    try:
        async with sessions() as db:
            await db.execute(statement)
            await db.commit()
    except Exception as exc:  # noqa: BLE001 — counting must never stop a reply
        logger.warning("could not count a use: %s", type(exc).__name__)


@dataclass
class PersonWeek:
    name: str
    role: str
    days: set[date] = field(default_factory=set)
    counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    cost: Decimal = Decimal(0)


async def week(db: AsyncSession, last_day: date) -> list[PersonWeek]:
    """Everyone's use over the seven days that end with last_day, members without any use included."""
    first_day = last_day - timedelta(days=WEEK_DAYS - 1)
    members = (await db.execute(select(AssistantMember).order_by(AssistantMember.created_at))).scalars().all()
    people = {m.telegram_id: PersonWeek(m.name, m.role) for m in members}
    rows = (
        await db.execute(select(AssistantUsage).where(AssistantUsage.day >= first_day, AssistantUsage.day <= last_day))
    ).scalars()
    for row in rows:
        person = people.get(row.member_id)
        if person is None:
            continue
        person.days.add(row.day)
        person.counts[row.feature] += row.count
        person.cost += row.cost_usd
    return list(people.values())


def _money(value: Decimal) -> str:
    return f"${value:.2f}"


def report_html(people: Sequence[PersonWeek], last_day: date, extra: Sequence[str] = ()) -> str:
    """The owner's week as a rich screen; `extra` (the models' health) folds away under a summary."""
    first_day = last_day - timedelta(days=WEEK_DAYS - 1)
    rows = []
    for person in sorted(people, key=lambda p: (-len(p.days), p.name)):
        used = sorted(person.counts.items(), key=lambda item: -item[1])[:4]
        details = ", ".join(f"{FEATURE_NAMES.get(name, name)} {count}" for name, count in used)
        rows.append((person.name, f"{len(person.days)} из {WEEK_DAYS}", details or "—", _money(person.cost)))
    total = sum((p.cost for p in people), Decimal(0))
    parts = [
        f"<h3>📊 Luna за неделю, {first_day:%d.%m}–{last_day:%d.%m}</h3>",
        rich_table(rows, header=("Кто", "Дней", "Чем пользовался", "$")),
        f"<p>Модели за неделю: {_money(total)}</p>",
    ]
    opened = {name for p in people for name in p.counts}
    unused = [FEATURE_NAMES[name] for name in OPTIONAL_FEATURES if name not in opened]
    if unused:
        parts.append(f"<p>Никто не открывал: {esc(', '.join(unused))}.</p>")
    lines = [line.strip() for line in extra if line.strip()]
    if lines:
        body = "".join(f"<p>{esc(line)}</p>" for line in lines[1:])
        parts.append(f"<details><summary>{esc(lines[0])}</summary>{body}</details>")
    screen: str = Rich("".join(parts))
    return screen


def report_due(now: datetime, sent_on: date | None) -> bool:
    return now.weekday() == REPORT_WEEKDAY and now.hour >= REPORT_HOUR and sent_on != now.date()


ExtraFn = Callable[[], Awaitable[list[str]]]


async def send_weekly_report(
    bot: Bot, sessions: async_sessionmaker[AsyncSession], now: datetime, extra: ExtraFn | None = None
) -> bool:
    """Send the week to the owner once on Sunday evening; True when it went out. `extra` adds lines, such as
    the models' health; it may take a minute, so it runs before the report is marked as sent."""
    async with sessions() as db:
        owner = await db.scalar(select(AssistantMember).where(AssistantMember.role == "owner"))
        if owner is None or not report_due(now, owner.report_sent_on):
            return False
    added: list[str] = []
    if extra is not None:
        try:
            added = await extra()
        except Exception as exc:  # noqa: BLE001 — the week's use goes out even if the extra block fails
            logger.warning("could not add to the weekly report: %s", type(exc).__name__)
    async with sessions() as db:
        owner = await db.scalar(select(AssistantMember).where(AssistantMember.role == "owner"))
        if owner is None or not report_due(now, owner.report_sent_on):
            return False
        text = report_html(await week(db, now.date()), now.date(), added)
        owner.report_sent_on = now.date()  # marked first: a Telegram failure loses one report, never repeats it
        await db.commit()
        owner_id = owner.telegram_id
    try:
        await send_long(bot, owner_id, text)
    except TelegramAPIError as exc:
        logger.warning("could not send the weekly report: %s", type(exc).__name__)
        return False
    return True


async def watch_weekly(
    bot: Bot,
    sessions: async_sessionmaker[AsyncSession],
    local_now: Callable[[], datetime],
    extra: ExtraFn | None = None,
) -> None:
    while True:
        await send_weekly_report(bot, sessions, local_now(), extra)
        await asyncio.sleep(REPORT_CHECK_SECONDS)
