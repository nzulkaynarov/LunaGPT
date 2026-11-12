"""Telegram helpers built on aiogram."""

import re
from contextlib import suppress
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import CallbackQuery, InputRichMessage, Message

MESSAGE_LIMIT = 4000  # Telegram rejects messages above 4096 characters
RICH_MESSAGE_LIMIT = 32000  # Telegram rejects rich messages above 32768 characters
# Code and display math stay as they are; an inline formula cannot open on a digit or a space, or close after a space.
DOLLARS = re.compile(r"```.*?```|`[^`\n]*`|\$\$.+?\$\$|\$(?![\d\s$])(?:[^$\n]*?[^\s$])?\$|\$", re.DOTALL)
CYRILLIC = re.compile(r"[А-Яа-яЁё]")
# A model's image link would be fetched and shown by Telegram, past the strict search children get.
EMBEDDED_MEDIA = re.compile(
    r"!\[[^\]]*\]\([^)]*\)|<(?:img|video|audio|iframe)\b[^>]*>(?:\s*</(?:video|audio|iframe)>)?", re.I
)


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


def _keep_or_escape_dollars(match: re.Match[str]) -> str:
    found = match.group()
    if found == "$" or (found[0] == "$" and found[:2] != "$$" and CYRILLIC.search(found)):
        return found.replace("$", r"\$")
    return found


def escape_prices(markdown: str) -> str:
    """Telegram reads any pair of dollars as a formula, so only what looks like math stays one."""
    return DOLLARS.sub(_keep_or_escape_dollars, markdown)


async def send_model_reply(bot: Bot, chat_id: int, markdown: str, reply_markup: Any = None) -> None:
    """Show a model's Markdown as Telegram's native rich message; plain text is the fallback."""
    text = escape_prices(EMBEDDED_MEDIA.sub("", markdown).strip() or markdown)
    try:
        await bot.send_rich_message(
            chat_id, InputRichMessage(markdown=text[:RICH_MESSAGE_LIMIT]), reply_markup=reply_markup
        )
    except TelegramBadRequest:
        await send_long(bot, chat_id, text, reply_markup)


def is_reply_to_prompt(message: Message, prompt: str) -> bool:
    """Only the bot can author its prompts, so a reply to one is an answer to it and never a chat turn."""
    reply = message.reply_to_message
    return bool(reply and reply.from_user and reply.from_user.is_bot and prompt in (reply.text or ""))


async def remove_menu(callback: CallbackQuery) -> None:
    """The menu whose button opened a "reply to this message" prompt goes away: the answer brings a fresh one."""
    if not isinstance(callback.message, Message):
        return
    with suppress(TelegramAPIError):  # too old to delete, or already gone
        await callback.message.delete()
