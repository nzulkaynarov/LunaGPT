"""Create the first invitation from the host: python -m lunagpt.assistant.invite owner Name."""

import asyncio
import sys

from aiogram import Bot

from lunagpt.assistant import access
from lunagpt.config import settings
from lunagpt.db import async_session_factory


async def main(role: str, name: str) -> None:
    async with Bot(settings.luna_telegram_bot_token) as bot:
        username = (await bot.get_me()).username
    async with async_session_factory() as db:
        token = await access.create_invite(db, name, role)
    print(f"https://t.me/{username}?start={token}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], " ".join(sys.argv[2:])))
