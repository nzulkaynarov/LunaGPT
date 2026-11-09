"""What people see before the first message: Telegram's description, short description and commands."""

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand

logger = logging.getLogger("lunagpt.assistant.profile")

DESCRIPTION = (
    "Luna — личный ИИ-ассистент для семьи и близких.\n\n"
    "💬 Отвечает на вопросы и ищет свежее в интернете\n"
    "🧠 Помнит важное о каждом, и эта память только твоя\n"
    "🏠 Хранит общие заметки семьи\n\n"
    "Нажми «Старт» и отправь свой контакт. Взрослый из семьи откроет доступ."
)
SHORT_DESCRIPTION = "Персональный помощник и член семьи"
COMMANDS = [
    BotCommand(command="start", description="Главное меню"),
    BotCommand(command="help", description="Что я умею"),
]


async def apply_profile(bot: Bot) -> None:
    """Telegram rate-limits these methods, so only a real difference is sent; a failure never stops the bot."""
    try:
        if (await bot.get_my_description()).description != DESCRIPTION:
            await bot.set_my_description(DESCRIPTION)
        if (await bot.get_my_short_description()).short_description != SHORT_DESCRIPTION:
            await bot.set_my_short_description(SHORT_DESCRIPTION)
        if await bot.get_my_commands() != COMMANDS:
            await bot.set_my_commands(COMMANDS)
    except TelegramAPIError as exc:
        logger.warning("could not update the bot profile: %s", type(exc).__name__)
