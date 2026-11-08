"""Mutual availability between the GPU host and the Raspberry Pi."""

import asyncio
import logging

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
            async with session_factory() as db:
                chat_ids = await access.adult_chat_ids(db)
            for chat_id in chat_ids:
                try:
                    await send_long(bot, chat_id, PI_DOWN_TEXT if event == "down" else PI_UP_TEXT)
                except TelegramAPIError as exc:
                    logger.warning("could not notify a parent: %s", type(exc).__name__)
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


async def sync_family_once(pi: PiClient, session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Follow renames, role changes and removals made on the Pi, and repair missing links."""
    people = await pi.people()
    async with session_factory() as db:
        missing_links = await access.reconcile_family(db, people)
    for person_id, telegram_id in missing_links:
        try:
            await pi.link_telegram(person_id, telegram_id)
        except PiConflictError:
            logger.warning("telegram account of person %s is linked to someone else", person_id)


async def watch_family(pi: PiClient, session_factory: async_sessionmaker[AsyncSession]) -> None:
    while True:
        try:
            await sync_family_once(pi, session_factory)
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
