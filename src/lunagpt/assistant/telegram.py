"""Telegram helpers built on aiogram."""

import html
import re
from collections.abc import Iterable, Sequence
from contextlib import suppress
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import CallbackQuery, InputRichMessage, Message

# Button colours (Bot API 9.4): green agrees or marks done, red refuses, switches off or deletes.
SUCCESS = "success"
DANGER = "danger"
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


class Rich(str):
    """A screen in Telegram Rich HTML (Bot API 10.1+); send_long and edit_screen show it as a rich message."""


def esc(value: object) -> str:
    """What people typed, safe inside Rich HTML."""
    return html.escape(str(value), quote=True)


def rich_table(rows: Iterable[Sequence[object]], header: Sequence[str] | None = None, caption: str = "") -> str:
    """A compact striped table; every cell is escaped."""
    parts = ["<table compact striped>"]
    if caption:
        parts.append(f"<caption>{esc(caption)}</caption>")
    if header:
        parts.append("<tr>" + "".join(f"<th>{esc(h)}</th>" for h in header) + "</tr>")
    parts += ["<tr>" + "".join(f"<td>{esc(c)}</td>" for c in row) + "</tr>" for row in rows]
    return "".join(parts) + "</table>"


_BREAKS = re.compile(r"<br\s*/?>|<hr\s*/?>|</(?:p|h\d|tr|li|summary|caption|blockquote|footer)>")
_CELL_END = re.compile(r"</t[dh]>")
_TAG = re.compile(r"<[^>]+>")
_CHECKED = re.compile(r'<input type="checkbox" checked\s*/?>')
_UNCHECKED = re.compile(r'<input type="checkbox"\s*/?>')


def to_plain(rich_html: str) -> str:
    """A rich screen as plain text: rows on lines, cells split by « · »."""
    text = _CHECKED.sub("✅ ", _UNCHECKED.sub("▫️ ", rich_html))
    text = _TAG.sub("", _BREAKS.sub("\n", _CELL_END.sub(" · ", text)))
    lines = [line.strip().removesuffix("·").strip() for line in html.unescape(text).split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


async def edit_screen(message: Message, text: str, reply_markup: Any = None) -> None:
    """Redraw a menu message in place; a Rich screen is drawn as a rich message, with plain text as the fallback."""
    try:
        if isinstance(text, Rich):
            try:
                await message.edit_text(rich_message=InputRichMessage(html=text), reply_markup=reply_markup)
                return
            except TelegramBadRequest as exc:
                if "not modified" in str(exc):
                    return
                text = to_plain(text)
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        if "not modified" not in str(exc):
            raise


async def send_long(bot: Bot, chat_id: int, text: str, reply_markup: Any = None) -> list[int]:
    """Send text in Telegram-sized parts, attaching the keyboard to the last one; the ids of the parts.

    A Rich screen goes out as one rich message, or as its plain text if Telegram refuses it."""
    if isinstance(text, Rich):
        if len(text) <= RICH_MESSAGE_LIMIT:
            try:
                shown = await bot.send_rich_message(chat_id, InputRichMessage(html=text), reply_markup=reply_markup)
                return [shown.message_id]
            except TelegramBadRequest:
                pass
        text = to_plain(text)
    parts = split_message(text)
    sent = [await bot.send_message(chat_id, part) for part in parts[:-1]]
    sent.append(await bot.send_message(chat_id, parts[-1], reply_markup=reply_markup))
    return [message.message_id for message in sent]


def _keep_or_escape_dollars(match: re.Match[str]) -> str:
    found = match.group()
    if found == "$" or (found[0] == "$" and found[:2] != "$$" and CYRILLIC.search(found)):
        return found.replace("$", r"\$")
    return found


def escape_prices(markdown: str) -> str:
    """Telegram reads any pair of dollars as a formula, so only what looks like math stays one."""
    return DOLLARS.sub(_keep_or_escape_dollars, markdown)


async def send_model_reply(bot: Bot, chat_id: int, markdown: str, reply_markup: Any = None) -> list[int]:
    """Show a model's Markdown as Telegram's native rich message; plain text is the fallback. The sent ids."""
    text = escape_prices(EMBEDDED_MEDIA.sub("", markdown).strip() or markdown)
    try:
        message = await bot.send_rich_message(
            chat_id, InputRichMessage(markdown=text[:RICH_MESSAGE_LIMIT]), reply_markup=reply_markup
        )
    except TelegramBadRequest:
        return await send_long(bot, chat_id, text, reply_markup)
    return [message.message_id]


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
