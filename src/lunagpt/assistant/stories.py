"""Stories about the family's cat Luna: a family member tells a habit or a funny moment (text, voice or a
photo with a caption), a parent approves it, and Luna remembers it as her own. Guests never see them."""

import logging
import random
from datetime import date

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lunagpt.assistant import access
from lunagpt.assistant.telegram import is_reply_to_prompt
from lunagpt.models import AssistantCatStory, AssistantMember

logger = logging.getLogger("lunagpt.assistant.stories")

STORY_PREFIXES = {"csa", "csl", "csy", "csn", "csx"}
ADULT_STORY_PREFIXES = {"csy", "csn", "csx"}
MIN_LENGTH = 10
MAX_LENGTH = 500
MAX_PENDING = 5
MAX_IN_PROMPT = 30
MAX_IN_PHRASE = 150
PAGE_SIZE = 10
BRIEF_EVERY_DAYS = 3
PROMPT = (
    "🐈 Расскажи про Луну: какая у неё повадка или какой был смешной случай. "
    "Можно текстом, голосом или фото с подписью.\nОтветь на это сообщение."
)
INTRO = (
    "🐈 Истории про Луну: {count}.\n"
    "Расскажи её повадку или смешной случай, и я буду помнить это как своё. "
    "Родители сначала посмотрят рассказ."
)
TOO_SHORT = f"Расскажи чуть подробнее: от {MIN_LENGTH} до {MAX_LENGTH} знаков. Попробуй ещё раз."
TOO_MANY = "У тебя уже есть истории, которые ждут родителей. Дождись их ответа 🙂"
SENT = "✨ Отправила историю родителям. Когда одобрят, я буду её помнить."
ADDED = "✅ Запомнила историю."
APPROVED_AUTHOR = "🎉 Родители одобрили твою историю, теперь я её помню:\n«{text}»"
REJECTED_AUTHOR = "Эту историю родители не добавили:\n«{text}»"
ALREADY_DECIDED = "Об этой истории уже решили."
NONE_YET = "Историй пока нет. Расскажи первую!"


def short(text: str, limit: int = MAX_IN_PHRASE) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def review_keyboard(story_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Добавить", callback_data=f"csy:{story_id}", style="success"),
                InlineKeyboardButton(text="❌ Не надо", callback_data=f"csn:{story_id}"),
            ]
        ]
    )


def intro_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✍️ Рассказать", callback_data="csa"),
                InlineKeyboardButton(text="📖 Почитать", callback_data="csl:0"),
            ]
        ]
    )


class StoryDesk:
    def __init__(self, bot: Bot, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._bot = bot
        self._sessions = sessions
        self._approved: list[tuple[str, str]] | None = None  # (text, author), oldest first; reset on change

    @staticmethod
    def is_reply(message: Message) -> bool:
        found: bool = is_reply_to_prompt(message, PROMPT)
        return found

    async def view(self, member: AssistantMember) -> None:
        count = len(await self.approved())
        await self._bot.send_message(member.telegram_id, INTRO.format(count=count), reply_markup=intro_keyboard())

    async def approved(self) -> list[tuple[str, str]]:
        if self._approved is None:
            async with self._sessions() as db:
                rows = await db.execute(
                    select(AssistantCatStory.text, AssistantMember.name)
                    .join(AssistantMember, AssistantMember.telegram_id == AssistantCatStory.author_id)
                    .where(AssistantCatStory.status == "approved")
                    .order_by(AssistantCatStory.id)
                )
                self._approved = [(text, name) for text, name in rows]
        return self._approved

    # ---------- what Luna does with them ----------

    async def memories(self) -> str:
        """The newest approved stories for the prompt, as data."""
        lines = [f"- {text} (рассказал(а) {name})" for text, name in (await self.approved())[-MAX_IN_PROMPT:]]
        return "\n".join(lines)

    async def phrases(self) -> list[str]:
        return [
            f"💭 Вспоминаю историю про себя:\n«{short(text)}»\n— автор: {name}" for text, name in await self.approved()
        ]

    async def brief_lines(self, member: AssistantMember, day: date) -> list[str]:
        """Every few days one story in the morning brief, a different one each time."""
        stories = await self.approved()
        if member.role not in access.FAMILY_ROLES or not stories or day.toordinal() % BRIEF_EVERY_DAYS:
            return []
        text, name = random.Random(day.toordinal()).choice(stories)
        return [f"\n🐈 А помнишь? «{short(text, 300)}» — автор: {name}"]

    # ---------- telling ----------

    async def ask(self, chat_id: int) -> None:
        await self._bot.send_message(chat_id, PROMPT, reply_markup=ForceReply())

    async def tell(self, member: AssistantMember, text: str, photo_file_id: str | None = None) -> None:
        text = " ".join(text.split())
        if not MIN_LENGTH <= len(text) <= MAX_LENGTH:
            await self._bot.send_message(member.telegram_id, TOO_SHORT)
            return
        adult = member.role in access.ADULT_ROLES
        async with self._sessions() as db:
            waiting = await db.scalar(
                select(func.count())
                .select_from(AssistantCatStory)
                .where(AssistantCatStory.author_id == member.telegram_id, AssistantCatStory.status == "pending")
            )
            if not adult and (waiting or 0) >= MAX_PENDING:
                await self._bot.send_message(member.telegram_id, TOO_MANY)
                return
            story = AssistantCatStory(
                author_id=member.telegram_id,
                text=text,
                photo_file_id=photo_file_id,
                status="approved" if adult else "pending",
            )
            db.add(story)
            await db.commit()
            parents = await access.adult_chat_ids(db)
        if adult:
            self._approved = None
            await self._bot.send_message(member.telegram_id, ADDED)
            return
        caption = f"🐈 {member.name} рассказывает про Луну:\n«{text}»"
        for parent in parents:
            try:
                if photo_file_id:
                    await self._bot.send_photo(
                        parent, photo_file_id, caption=caption, reply_markup=review_keyboard(story.id)
                    )
                else:
                    await self._bot.send_message(parent, caption, reply_markup=review_keyboard(story.id))
            except TelegramAPIError as exc:
                logger.warning("could not ask a parent about a story: %s", type(exc).__name__)
        await self._bot.send_message(member.telegram_id, SENT)

    # ---------- buttons ----------

    async def handle(self, callback: CallbackQuery, member: AssistantMember, prefix: str, args: list[str]) -> None:
        if prefix == "csa":
            await self.ask(member.telegram_id)
            return
        if prefix == "csl":
            await self._list(callback, member, int(args[0]))
            return
        story_id = int(args[0])
        async with self._sessions() as db:
            story = await db.get(AssistantCatStory, story_id)
        if story is None or (prefix != "csx" and story.status != "pending"):
            await self._edit(callback, ALREADY_DECIDED, None)
            return
        if prefix == "csy":
            async with self._sessions() as db:
                found = await db.get(AssistantCatStory, story_id)
                if found is not None:
                    found.status = "approved"
                    await db.commit()
            self._approved = None
            remove = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🗑 Убрать", callback_data=f"csx:{story_id}")]]
            )
            await self._edit(callback, f"✅ Добавлено: «{short(story.text)}»", remove)
            await self._tell_author(story.author_id, APPROVED_AUTHOR.format(text=short(story.text)))
            return
        async with self._sessions() as db:
            await db.execute(delete(AssistantCatStory).where(AssistantCatStory.id == story_id))
            await db.commit()
        self._approved = None
        if prefix == "csn":
            await self._edit(callback, f"❌ Не добавлено: «{short(story.text)}»", None)
            await self._tell_author(story.author_id, REJECTED_AUTHOR.format(text=short(story.text)))
        else:
            await self._edit(callback, f"🗑 Убрано: «{short(story.text)}»", None)

    async def _list(self, callback: CallbackQuery, member: AssistantMember, page: int) -> None:
        async with self._sessions() as db:
            rows = list(
                await db.execute(
                    select(AssistantCatStory.id, AssistantCatStory.text, AssistantMember.name)
                    .join(AssistantMember, AssistantMember.telegram_id == AssistantCatStory.author_id)
                    .where(AssistantCatStory.status == "approved")
                    .order_by(AssistantCatStory.id.desc())
                    .offset(page * PAGE_SIZE)
                    .limit(PAGE_SIZE + 1)
                )
            )
        if not rows and page == 0:
            await self._bot.send_message(member.telegram_id, NONE_YET)
            return
        shown = rows[:PAGE_SIZE]
        lines = [f"{n}. {short(text)} — {name}" for n, (_, text, name) in enumerate(shown, page * PAGE_SIZE + 1)]
        buttons: list[list[InlineKeyboardButton]] = []
        if member.role in access.ADULT_ROLES:
            buttons += [
                [InlineKeyboardButton(text=f"🗑 {n}", callback_data=f"csx:{story_id}")]
                for n, (story_id, _, _) in enumerate(shown, page * PAGE_SIZE + 1)
            ]
        if len(rows) > PAGE_SIZE:
            buttons.append([InlineKeyboardButton(text="Дальше →", callback_data=f"csl:{page + 1}")])
        await self._bot.send_message(
            member.telegram_id,
            "📖 Истории про Луну:\n\n" + "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None,
        )

    async def _tell_author(self, chat_id: int, text: str) -> None:
        try:
            await self._bot.send_message(chat_id, text)
        except TelegramAPIError as exc:
            logger.warning("could not tell the author about a story: %s", type(exc).__name__)

    @staticmethod
    async def _edit(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None) -> None:
        message = callback.message
        if not isinstance(message, Message):
            return
        if message.photo:
            await message.edit_caption(caption=text, reply_markup=markup)
        else:
            await message.edit_text(text, reply_markup=markup)
