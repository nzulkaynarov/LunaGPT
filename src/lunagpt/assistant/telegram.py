"""Telegram helpers built on aiogram."""

from typing import Any

from aiogram import Bot
from aiogram.types import Message

MESSAGE_LIMIT = 4000  # Telegram rejects messages above 4096 characters


def split_message(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Split long text for Telegram, preferring line breaks."""
    parts: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    if rest or not parts:
        parts.append(rest)
    return parts


async def send_long(bot: Bot, chat_id: int, text: str, reply_markup: Any = None) -> None:
    """Send text in Telegram-sized parts, attaching the keyboard to the last one."""
    parts = split_message(text)
    for part in parts[:-1]:
        await bot.send_message(chat_id, part)
    await bot.send_message(chat_id, parts[-1], reply_markup=reply_markup)


def is_reply_to_prompt(message: Message, prompt: str) -> bool:
    """Only the bot can author its prompts, so a reply to one is an answer to it and never a chat turn."""
    reply = message.reply_to_message
    return bool(reply and reply.from_user and reply.from_user.is_bot and prompt in (reply.text or ""))
