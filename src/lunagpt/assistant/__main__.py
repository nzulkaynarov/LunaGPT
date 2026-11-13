"""Entry point: python -m lunagpt.assistant."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from aiogram import Bot, Dispatcher

from lunagpt.assistant import usage
from lunagpt.assistant.bot import LunaBot, build_router, watch_time_requests
from lunagpt.assistant.llm import generate_quiz, generate_reply
from lunagpt.assistant.memories import Memory
from lunagpt.assistant.monitor import send_heartbeats, supervise, watch_family, watch_pi
from lunagpt.assistant.pi_client import PiClient
from lunagpt.assistant.profile import apply_profile
from lunagpt.config import settings
from lunagpt.db import async_session_factory

logger = logging.getLogger("lunagpt.assistant")


def local_now() -> datetime:
    """The family's wall clock, without a timezone, as the weekly report compares hours and days."""
    return (datetime.now(UTC) + timedelta(hours=settings.luna_utc_offset_hours)).replace(tzinfo=None)


async def main() -> None:
    if not settings.luna_telegram_bot_token or not settings.litellm_assistant_key:
        raise SystemExit("LUNA_TELEGRAM_BOT_TOKEN and LITELLM_ASSISTANT_KEY are required")
    bot = Bot(settings.luna_telegram_bot_token)
    me = await bot.get_me()
    await apply_profile(bot)
    pi = PiClient()
    memory = Memory(async_session_factory)
    luna = LunaBot(bot, async_session_factory, generate_reply, generate_quiz, pi, str(me.username), memory)
    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(luna))
    logger.info("Luna AI Assistant started as @%s", me.username)
    await asyncio.gather(
        dispatcher.start_polling(bot, allowed_updates=["message", "callback_query"], handle_signals=False),
        supervise("pi", lambda: watch_pi(bot, pi, async_session_factory)),
        supervise("heartbeats", lambda: send_heartbeats(pi, async_session_factory)),
        supervise("family", lambda: watch_family(bot, pi, async_session_factory)),
        supervise("time requests", lambda: watch_time_requests(bot, pi, async_session_factory)),
        supervise("reminders", luna.reminders.watch),
        supervise("updates", luna.watch_updates),
        supervise("weekly report", lambda: usage.watch_weekly(bot, async_session_factory, local_now)),
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    asyncio.run(main())
