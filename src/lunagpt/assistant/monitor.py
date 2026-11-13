"""Mutual availability between the GPU host and the Raspberry Pi."""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lunagpt.assistant import access
from lunagpt.assistant.pi_client import PiClient, PiConflictError, PiNotFoundError, PiUnavailableError
from lunagpt.assistant.telegram import send_long

logger = logging.getLogger("lunagpt.assistant.monitor")

CHECK_INTERVAL_SECONDS = 60
HEARTBEAT_INTERVAL_SECONDS = 30
FAMILY_SYNC_INTERVAL_SECONDS = 300
FAILURES_BEFORE_ALERT = 3
RESTART_DELAY_SECONDS = 30
PI_DOWN_TEXT = "⚠️ HomeSec на Raspberry Pi не отвечает. Команды сети могут быть недоступны. Luna работает."
PI_UP_TEXT = "✅ HomeSec на Raspberry Pi снова на связи."


class AvailabilityTracker:
    """Reports a single transition after sustained failure, so restarts stay silent."""

    def __init__(self, failures_before_alert: int = FAILURES_BEFORE_ALERT) -> None:
        self._threshold = failures_before_alert
        self._failures = 0
        self._down = False

    def observe(self, healthy: bool) -> str | None:
        """Return "down" or "up" when the state changes, otherwise None."""
        if healthy:
            self._failures = 0
            if self._down:
                self._down = False
                return "up"
            return None
        self._failures += 1
        if not self._down and self._failures >= self._threshold:
            self._down = True
            return "down"
        return None


async def watch_pi(bot: Bot, pi: PiClient, session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Tell the adults when the Pi stops or returns."""
    tracker = AvailabilityTracker()
    while True:
        event = tracker.observe(await pi.is_healthy())
        if event is not None:
            logger.warning("raspberry pi state changed: %s", event)
            await _tell_adults(bot, session_factory, PI_DOWN_TEXT if event == "down" else PI_UP_TEXT)
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


def left_family_text(name: str) -> str:
    return (
        f"👤 {name}: такого человека больше нет в HomeSec, поэтому доступ в Luna теперь гостевой. "
        "Память, напоминания и списки сохранены.\n"
        "Вернуть в семью или убрать совсем можно в «🏠 Семья» → «👥 Люди»."
    )


async def _tell_adults(bot: Bot, session_factory: async_sessionmaker[AsyncSession], text: str) -> None:
    async with session_factory() as db:
        chat_ids = await access.adult_chat_ids(db)
    for chat_id in chat_ids:
        try:
            await send_long(bot, chat_id, text)
        except TelegramAPIError as exc:
            logger.warning("could not notify a parent: %s", type(exc).__name__)


async def sync_family_once(bot: Bot, pi: PiClient, session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Follow renames, role changes and removals made on the Pi, and repair missing links."""
    people = await pi.people()
    async with session_factory() as db:
        result = await access.reconcile_family(db, people)
    if result.refused:
        logger.warning("family sync skipped: the Pi's people list looks empty, malformed or reset")
        return
    for name in result.left_family:
        logger.warning("a member left the family on the Pi and became a guest")
        await _tell_adults(bot, session_factory, left_family_text(name))
    for person_id, telegram_id in result.missing_links:
        try:
            await pi.link_telegram(person_id, telegram_id)
        except PiConflictError:
            logger.warning("telegram account of person %s is linked to someone else", person_id)


async def watch_family(bot: Bot, pi: PiClient, session_factory: async_sessionmaker[AsyncSession]) -> None:
    while True:
        async with session_factory() as db:
            await access.expire_applications(db)  # does not depend on the Pi being up
        try:
            await sync_family_once(bot, pi, session_factory)
        except (PiUnavailableError, PiNotFoundError) as exc:
            logger.warning("family sync failed: %s", type(exc).__name__)
        await asyncio.sleep(FAMILY_SYNC_INTERVAL_SECONDS)


async def send_heartbeats(pi: PiClient, session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Prove to the Pi that Luna is alive and share who to tell if it stops."""
    while True:
        try:
            async with session_factory() as db:
                members = await access.list_members(db)
            await pi.heartbeat([m.telegram_id for m in members])
        except (PiUnavailableError, PiConflictError, PiNotFoundError) as exc:
            logger.warning("heartbeat failed: %s", type(exc).__name__)
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)


async def supervise(
    name: str, job: Callable[[], Awaitable[None]], restart_delay: float = RESTART_DELAY_SECONDS
) -> None:
    """Keep a background loop alive: an unexpected error is logged and the loop starts again."""
    while True:
        try:
            await job()
            logger.warning("background job %s returned; starting it again", name)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("background job %s failed; starting it again in %ss", name, restart_delay)
        await asyncio.sleep(restart_delay)
