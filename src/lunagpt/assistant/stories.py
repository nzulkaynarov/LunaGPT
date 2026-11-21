"""Stories about the family's cat Luna: a family member tells a habit or a funny moment (text, voice or a
photo with a caption), a parent approves it, and Luna remembers it as her own. Guests never see them."""

import logging
import random
from datetime import date

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lunagpt.assistant import access
from lunagpt.assistant.telegram import DANGER, SUCCESS, edit_screen, is_reply_to_prompt, remove_menu, send_prompt
from lunagpt.models import AssistantCatStory, AssistantMember

logger = logging.getLogger("lunagpt.assistant.stories")

STORY_PREFIXES = {"csi", "csa", "csl", "csy", "csn", "csx", "csq"}
STORY_CANCEL = "csq"  # "✖ Отмена" under the story prompt: the stories screen, removed then, comes back
PHRASE_ASK = "cpa"  # a child's "make up a phrase" button lives here, next to the stories
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
TOO_SHORT = f"Расскажи чуть подробнее: хотя бы {MIN_LENGTH} знаков."
NO_CAPTION = "Пришли фото ещё раз с подписью: что на нём происходит."
TOO_LONG = f"Слишком длинно: уложись в {MAX_LENGTH} знаков."
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
                InlineKeyboardButton(text="✅ Добавить", callback_data=f"csy:{story_id}", style=SUCCESS),
                InlineKeyboardButton(text="❌ Не надо", callback_data=f"csn:{story_id}"),
            ]
        ]
    )


def back_to_stories() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="← Назад", callback_data="csi")]])


def intro_keyboard(role: str) -> InlineKeyboardMarkup:
    """Stories sit inside "🧠 Память", so the screen leads back there."""
    rows = [
        [
            InlineKeyboardButton(text="✍️ Рассказать", callback_data="csa"),
            InlineKeyboardButton(text="📖 Почитать", callback_data="csl:0"),
        ]
    ]
    if role == "kid":
        rows.append([InlineKeyboardButton(text="😺 Фраза для Луны", callback_data=PHRASE_ASK)])
    rows.append([InlineKeyboardButton(text="← Память", callback_data="mem:0")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


class StoryDesk:
    def __init__(self, bot: Bot, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._bot = bot
        self._sessions = sessions
        self._approved: list[tuple[str, str]] | None = None  # (text, author), oldest first; reset on change

    @staticmethod
    def is_reply(message: Message) -> bool:
        found: bool = is_reply_to_prompt(message, PROMPT)
        return found

    async def intro(self, member: AssistantMember) -> tuple[str, InlineKeyboardMarkup]:
        return INTRO.format(count=len(await self.approved())), intro_keyboard(member.role)

    async def view(self, member: AssistantMember, note: str = "") -> None:
        """The stories screen as a new message; a note on top says what just happened."""
        text, markup = await self.intro(member)
        await self._bot.send_message(member.telegram_id, f"{note}\n\n{text}" if note else text, reply_markup=markup)

    async def _waiting_too_many(self, db: AsyncSession, member: AssistantMember) -> bool:
        """A child may have only a few stories waiting for the parents; adults' stories never wait."""
        if member.role in access.ADULT_ROLES:
            return False
        waiting = await db.scalar(
            select(func.count())
            .select_from(AssistantCatStory)
            .where(AssistantCatStory.author_id == member.telegram_id, AssistantCatStory.status == "pending")
        )
        return (waiting or 0) >= MAX_PENDING

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

    async def ask(self, chat_id: int, problem: str = "") -> None:
        await send_prompt(self._bot, chat_id, PROMPT, STORY_CANCEL, problem)

    async def tell(self, member: AssistantMember, text: str, photo_file_id: str | None = None) -> None:
        """The answer to the story prompt; the stories screen the prompt replaced comes back with the outcome."""
        text = " ".join(text.split())
        if photo_file_id and not text:
            await self.ask(member.telegram_id, NO_CAPTION)
            return
        if not MIN_LENGTH <= len(text) <= MAX_LENGTH:
            await self.ask(member.telegram_id, TOO_SHORT if len(text) < MIN_LENGTH else TOO_LONG)
            return
        adult = member.role in access.ADULT_ROLES
        async with self._sessions() as db:
            if await self._waiting_too_many(db, member):  # the button checks first; this is an older prompt
                await self._bot.send_message(member.telegram_id, TOO_MANY, reply_markup=back_to_stories())
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
            await self.view(member, ADDED)
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
        await self.view(member, SENT)

    # ---------- buttons ----------

    async def handle(self, callback: CallbackQuery, member: AssistantMember, prefix: str, args: list[str]) -> None:
        if prefix == "csi":
            await self._edit(callback, *await self.intro(member))
            return
        if prefix == "csa":
            async with self._sessions() as db:
                too_many = await self._waiting_too_many(db, member)
            if too_many:  # said before anything is typed, so no story is lost
                await self._edit(callback, TOO_MANY, back_to_stories())
                return
            await self.ask(member.telegram_id)
            await remove_menu(callback)
            return
        if prefix == STORY_CANCEL:
            await remove_menu(callback)
            await self.view(member)
            return
        if prefix == "csl":
            await self._edit(callback, *await self._list(member, int(args[0])))
            return
        story_id = int(args[0])
        async with self._sessions() as db:
            story = await db.get(AssistantCatStory, story_id)
        page = int(args[1]) if prefix == "csx" and len(args) > 1 else None  # 🗑 pressed under the list
        if story is None and page is not None:
            await self._edit(callback, *await self._list(member, page))
            return
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
                inline_keyboard=[[InlineKeyboardButton(text="🗑 Убрать", callback_data=f"csx:{story_id}", style=DANGER)]]
            )
            await self._edit(callback, f"✅ Добавлено: «{short(story.text)}»", remove)
            await self._tell_author(story.author_id, APPROVED_AUTHOR.format(text=short(story.text)))
            return
        async with self._sessions() as db:
            await db.execute(delete(AssistantCatStory).where(AssistantCatStory.id == story_id))
            await db.commit()
        self._approved = None
        removed = f"🗑 Убрано: «{short(story.text)}»"
        if prefix == "csn":
            await self._edit(callback, f"❌ Не добавлено: «{short(story.text)}»", None)
            await self._tell_author(story.author_id, REJECTED_AUTHOR.format(text=short(story.text)))
        elif page is not None:
            text, markup = await self._list(member, page)
            await self._edit(callback, f"{removed}\n\n{text}", markup)
        else:
            await self._edit(callback, removed, None)

    async def _page(self, page: int) -> list[tuple[int, str, str]]:
        """One page of approved stories, newest first, with one more row to tell whether a next page exists."""
        async with self._sessions() as db:
            rows = await db.execute(
                select(AssistantCatStory.id, AssistantCatStory.text, AssistantMember.name)
                .join(AssistantMember, AssistantMember.telegram_id == AssistantCatStory.author_id)
                .where(AssistantCatStory.status == "approved")
                .order_by(AssistantCatStory.id.desc())
                .offset(page * PAGE_SIZE)
                .limit(PAGE_SIZE + 1)
            )
            return [(story_id, text, name) for story_id, text, name in rows]

    async def _list(self, member: AssistantMember, page: int) -> tuple[str, InlineKeyboardMarkup]:
        """The stories screen, drawn in place of the menu; it always leads back to the stories."""
        page = max(page, 0)
        rows = await self._page(page)
        while not rows and page > 0:  # the last story of this page was removed
            page -= 1
            rows = await self._page(page)
        back = [InlineKeyboardButton(text="← Назад", callback_data="csi")]
        if not rows:
            tell = [InlineKeyboardButton(text="✍️ Рассказать", callback_data="csa")]
            return NONE_YET, InlineKeyboardMarkup(inline_keyboard=[tell, back])
        shown = rows[:PAGE_SIZE]
        first = page * PAGE_SIZE + 1
        lines = [f"{n}. {short(text)} — {name}" for n, (_, text, name) in enumerate(shown, first)]
        buttons: list[list[InlineKeyboardButton]] = []
        if member.role in access.ADULT_ROLES:
            buttons += [
                [InlineKeyboardButton(text=f"🗑 {n}", callback_data=f"csx:{story_id}:{page}", style=DANGER)]
                for n, (story_id, _, _) in enumerate(shown, first)
            ]
        navigation = []
        if page > 0:
            navigation.append(InlineKeyboardButton(text="← Раньше", callback_data=f"csl:{page - 1}"))
        if len(rows) > PAGE_SIZE:
            navigation.append(InlineKeyboardButton(text="Дальше →", callback_data=f"csl:{page + 1}"))
        if navigation:
            buttons.append(navigation)
        buttons.append(back)
        return "📖 Истории про Луну:\n\n" + "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)

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
            await edit_screen(message, text, markup)
