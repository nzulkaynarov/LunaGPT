"""Children make up waiting phrases for Luna; a parent approves each one before the family sees it."""

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lunagpt.assistant import access
from lunagpt.assistant.telegram import is_reply_to_prompt
from lunagpt.models import AssistantCatPhrase, AssistantMember

logger = logging.getLogger("lunagpt.assistant.phrases")

PHRASE_PREFIXES = {"cpy", "cpn", "cpx"}
MIN_LENGTH = 5
MAX_LENGTH = 100
MAX_PENDING = 3
PROMPT = (
    "😺 Придумай, что Луна пишет, пока думает над ответом. Например: «🐾 Ловлю ответ за хвост…»\n"
    "Напиши фразу ответом на это сообщение."
)
TOO_SHORT = f"Фраза должна быть от {MIN_LENGTH} до {MAX_LENGTH} знаков, в одну строку. Попробуй ещё раз."
TOO_MANY = "У тебя уже есть фразы, которые ждут родителей. Дождись их ответа 🙂"
SENT = "✨ Отправила фразу родителям. Если одобрят, она будет появляться, пока я думаю."
APPROVED_KID = "🎉 Родители одобрили твою фразу! Теперь она появляется, пока я думаю:\n«{text}»"
REJECTED_KID = "Эту фразу родители не добавили:\n«{text}»\nПридумай другую 🙂"
ALREADY_DECIDED = "Об этой фразе уже решили."


def review_keyboard(phrase_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Добавить", callback_data=f"cpy:{phrase_id}", style="success"),
                InlineKeyboardButton(text="❌ Не надо", callback_data=f"cpn:{phrase_id}"),
            ]
        ]
    )


def shown(text: str, author: str) -> str:
    return f"{text}\n— автор: {author}"


class PhraseDesk:
    def __init__(self, bot: Bot, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._bot = bot
        self._sessions = sessions
        self._approved: list[str] | None = None  # cached until a parent approves or removes one

    @staticmethod
    def is_reply(message: Message) -> bool:
        found: bool = is_reply_to_prompt(message, PROMPT)
        return found

    async def ask(self, member: AssistantMember) -> None:
        await self._bot.send_message(member.telegram_id, PROMPT, reply_markup=ForceReply())

    async def suggest(self, member: AssistantMember, text: str) -> None:
        text = text.strip()
        if not MIN_LENGTH <= len(text) <= MAX_LENGTH or "\n" in text:
            await self._bot.send_message(member.telegram_id, TOO_SHORT)
            return
        async with self._sessions() as db:
            waiting = await db.scalar(
                select(func.count())
                .select_from(AssistantCatPhrase)
                .where(AssistantCatPhrase.author_id == member.telegram_id, AssistantCatPhrase.status == "pending")
            )
            if (waiting or 0) >= MAX_PENDING:
                await self._bot.send_message(member.telegram_id, TOO_MANY)
                return
            phrase = AssistantCatPhrase(author_id=member.telegram_id, text=text)
            db.add(phrase)
            await db.commit()
            parents = await access.adult_chat_ids(db)
        if member.role in access.ADULT_ROLES:
            await self._approve(phrase.id)
            await self._bot.send_message(member.telegram_id, "✅ Добавила фразу.")
            return
        for parent in parents:
            try:
                await self._bot.send_message(
                    parent,
                    f"😺 {member.name} придумывает фразу для Луны:\n«{text}»",
                    reply_markup=review_keyboard(phrase.id),
                )
            except TelegramAPIError as exc:
                logger.warning("could not ask a parent about a phrase: %s", type(exc).__name__)
        await self._bot.send_message(member.telegram_id, SENT)

    async def family_phrases(self) -> list[str]:
        if self._approved is None:
            async with self._sessions() as db:
                rows = await db.execute(
                    select(AssistantCatPhrase.text, AssistantMember.name)
                    .join(AssistantMember, AssistantMember.telegram_id == AssistantCatPhrase.author_id)
                    .where(AssistantCatPhrase.status == "approved")
                )
                self._approved = [shown(text, name) for text, name in rows]
        return self._approved

    async def _approve(self, phrase_id: int) -> None:
        async with self._sessions() as db:
            phrase = await db.get(AssistantCatPhrase, phrase_id)
            if phrase is not None:
                phrase.status = "approved"
                await db.commit()
        self._approved = None

    async def handle(self, callback: CallbackQuery, member: AssistantMember, prefix: str, args: list[str]) -> None:
        phrase_id = int(args[0])
        async with self._sessions() as db:
            phrase = await db.get(AssistantCatPhrase, phrase_id)
        if phrase is None or (prefix != "cpx" and phrase.status != "pending"):
            await self._edit(callback, ALREADY_DECIDED, None)
            return
        if prefix == "cpy":
            await self._approve(phrase_id)
            remove = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🗑 Убрать", callback_data=f"cpx:{phrase_id}")]]
            )
            await self._edit(callback, f"✅ Добавлено: «{phrase.text}»", remove)
            await self._tell(phrase.author_id, APPROVED_KID.format(text=phrase.text))
            return
        async with self._sessions() as db:
            await db.execute(delete(AssistantCatPhrase).where(AssistantCatPhrase.id == phrase_id))
            await db.commit()
        self._approved = None
        if prefix == "cpn":
            await self._edit(callback, f"❌ Не добавлено: «{phrase.text}»", None)
            await self._tell(phrase.author_id, REJECTED_KID.format(text=phrase.text))
        else:
            await self._edit(callback, f"🗑 Убрано: «{phrase.text}»", None)

    async def _tell(self, chat_id: int, text: str) -> None:
        try:
            await self._bot.send_message(chat_id, text)
        except TelegramAPIError as exc:
            logger.warning("could not tell the author about a phrase: %s", type(exc).__name__)

    @staticmethod
    async def _edit(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None) -> None:
        if isinstance(callback.message, Message):
            await callback.message.edit_text(text, reply_markup=markup)
