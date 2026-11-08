"""Entry point: python -m lunagpt.assistant."""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand

from lunagpt.assistant.bot import COMMANDS, LunaBot, build_router, watch_time_requests
from lunagpt.assistant.llm import generate_quiz, generate_reply
from lunagpt.assistant.monitor import send_heartbeats, watch_family, watch_pi
from lunagpt.assistant.pi_client import PiClient
from lunagpt.config import settings
from lunagpt.db import async_session_factory

logger = logging.getLogger("lunagpt.assistant")


async def main() -> None:
    if not settings.luna_telegram_bot_token or not settings.litellm_assistant_key:
        raise SystemExit("LUNA_TELEGRAM_BOT_TOKEN and LITELLM_ASSISTANT_KEY are required")
    bot = Bot(settings.luna_telegram_bot_token)
    me = await bot.get_me()
    await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in COMMANDS])
    pi = PiClient()
    luna = LunaBot(bot, async_session_factory, generate_reply, generate_quiz, pi, str(me.username))
    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(luna))
    logger.info("Luna AI Assistant started as @%s", me.username)
    await asyncio.gather(
        dispatcher.start_polling(bot, allowed_updates=["message", "callback_query"], handle_signals=False),
        watch_pi(bot, pi, async_session_factory),
        send_heartbeats(pi, async_session_factory),
        watch_family(pi, async_session_factory),
        watch_time_requests(bot, pi, async_session_factory),
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    asyncio.run(main())
