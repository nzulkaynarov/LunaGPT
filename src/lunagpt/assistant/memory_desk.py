"""🧠 Память: what Luna remembers about one person (👤) and what the family keeps for everyone (🏠).

A person sees and deletes only their own facts. Family notes are seen by the whole family; an adult or
the note's author may delete one, and that right is checked again when the button is pressed."""

import uuid
from collections.abc import Callable

from aiogram import Bot
from aiogram.types import CallbackQuery, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Message

from lunagpt.assistant import access
from lunagpt.assistant.memories import FAMILY_DUPLICATE, FAMILY_FAILED, FAMILY_FULL, FAMILY_SAVED, Memory
from lunagpt.assistant.telegram import remove_menu, send_long
from lunagpt.models import AssistantMember

MEMORY_PREFIXES = {"mem", "memd", "memc", "memy"}
FAMILY_MEMORY_PREFIXES = {"fam", "fama", "famd", "famy"}
MEMORY_PAGE_SIZE = 8
BTN_STORY = "🐈 Истории про Луну"
FAMILY_PROMPT = (
    "Напиши ответом на это сообщение, что запомнить для всей семьи. "
    "Это увидят все в семье, включая детей, поэтому не пиши пароли и номера карт."
)
FAMILY_REPLIES = {
    FAMILY_SAVED: "Запомнила для всей семьи 🏠",
    FAMILY_DUPLICATE: "Это уже есть в общей памяти.",
    FAMILY_FULL: "Общая память заполнена. Удали что-нибудь лишнее и попробуй снова.",
    FAMILY_FAILED: "Не получилось сохранить, попробуй чуть позже.",
}


class MemoryDesk:
    """The memory screens and their buttons."""

    def __init__(self, bot: Bot, memory: Memory | None, forget_conversation: Callable[[int], None]) -> None:
        self._bot = bot
        self.memory = memory
        self._forget_conversation = forget_conversation

    async def _say(self, chat_id: int, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
        await send_long(self._bot, chat_id, text, reply_markup)

    @staticmethod
    async def _edit(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None) -> None:
        if isinstance(callback.message, Message):
            await callback.message.edit_text(text, reply_markup=markup)

    async def handle(self, callback: CallbackQuery, member: AssistantMember, prefix: str, args: list[str]) -> None:
        user_id = member.telegram_id
        if prefix == "mem":
            await self._edit(callback, *await self.view(member, int(args[0])))
        elif prefix == "memd":
            await self._forget_fact(callback, member, args[0], int(args[1]))
        elif prefix == "memc":
            await self._edit(callback, *self._confirm_forget_all())
        elif prefix == "memy":
            await self._forget_everything(callback, user_id)
        elif prefix == "fam":
            await self._edit(callback, *await self.family_view(member, int(args[0])))
        elif prefix == "fama":
            await self._bot.send_message(user_id, FAMILY_PROMPT, reply_markup=ForceReply())
            await remove_menu(callback)
        elif prefix == "famd":
            await self._edit(callback, *await self._confirm_family_delete(member, args[0]))
        elif prefix == "famy":
            await self.forget_family_fact(callback, member, args[0])

    async def save_family_note(self, member: AssistantMember, note: str) -> None:
        if self.memory is None:
            await self._say(member.telegram_id, FAMILY_REPLIES[FAMILY_FAILED])
            return
        outcome = await self.memory.family.add(member.telegram_id, member.name, note)
        await self._say(member.telegram_id, FAMILY_REPLIES[outcome])

    @staticmethod
    def _may_delete(member: AssistantMember, author_id: int) -> bool:
        return member.role in access.ADULT_ROLES or member.telegram_id == author_id

    async def family_view(self, member: AssistantMember, page: int) -> tuple[str, InlineKeyboardMarkup | None]:
        if self.memory is None:
            return "Память сейчас недоступна.", None
        facts = await self.memory.family.list_facts()
        last_page = max(len(facts) - 1, 0) // MEMORY_PAGE_SIZE
        page = max(0, min(page, last_page))
        shown = facts[page * MEMORY_PAGE_SIZE : (page + 1) * MEMORY_PAGE_SIZE]
        first = page * MEMORY_PAGE_SIZE + 1
        lines = [f"{first + i}. {fact.content} ({fact.author_name})" for i, fact in enumerate(shown)]
        text = (
            f"🏠 Общая память семьи ({len(facts)}):\n\n" + "\n".join(lines) if facts else "🏠 Общая память пока пуста."
        )
        delete_buttons = [
            InlineKeyboardButton(text=f"🗑 {first + i}", callback_data=f"famd:{fact.id.hex}")
            for i, fact in enumerate(shown)
            if self._may_delete(member, fact.author_id)
        ]
        rows = [delete_buttons[i : i + 4] for i in range(0, len(delete_buttons), 4)]
        navigation = []
        if page > 0:
            navigation.append(InlineKeyboardButton(text="←", callback_data=f"fam:{page - 1}"))
        if page < last_page:
            navigation.append(InlineKeyboardButton(text="→", callback_data=f"fam:{page + 1}"))
        if navigation:
            rows.append(navigation)
        rows.append([InlineKeyboardButton(text="➕ Добавить", callback_data="fama")])
        rows.append([InlineKeyboardButton(text="← Назад", callback_data="mem:0")])
        return text, InlineKeyboardMarkup(inline_keyboard=rows)

    async def _confirm_family_delete(
        self, member: AssistantMember, memory_hex: str
    ) -> tuple[str, InlineKeyboardMarkup | None]:
        fact = await self.memory.family.get(uuid.UUID(hex=memory_hex)) if self.memory else None
        if fact is None or not self._may_delete(member, fact.author_id):
            return await self.family_view(member, 0)
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Да, удалить", callback_data=f"famy:{memory_hex}", style="danger")],
                [InlineKeyboardButton(text="← Назад", callback_data="fam:0")],
            ]
        )
        return f"Удалить для всей семьи?\n\n{fact.content}", markup

    async def forget_family_fact(self, callback: CallbackQuery, member: AssistantMember, memory_hex: str) -> None:
        """The right to delete is checked again here, since callback data can outlive a role change."""
        memory_id = uuid.UUID(hex=memory_hex)
        fact = await self.memory.family.get(memory_id) if self.memory else None
        if self.memory is not None and fact is not None and self._may_delete(member, fact.author_id):
            await self.memory.family.forget(memory_id)
        await self._edit(callback, *await self.family_view(member, 0))

    def _with_family_button(
        self, member: AssistantMember, view: tuple[str, InlineKeyboardMarkup | None]
    ) -> tuple[str, InlineKeyboardMarkup | None]:
        if member.role not in access.FAMILY_ROLES:
            return view
        text, markup = view
        rows = [*(markup.inline_keyboard if markup else [])]
        rows.append(
            [
                InlineKeyboardButton(text="🏠 Общая память", callback_data="fam:0"),
                InlineKeyboardButton(text=BTN_STORY, callback_data="csi"),
            ]
        )
        return text, InlineKeyboardMarkup(inline_keyboard=rows)

    async def view(self, member: AssistantMember, page: int) -> tuple[str, InlineKeyboardMarkup | None]:
        return self._with_family_button(member, await self._personal_view(member.telegram_id, page))

    async def _personal_view(self, user_id: int, page: int) -> tuple[str, InlineKeyboardMarkup | None]:
        if self.memory is None:
            return "Память сейчас недоступна.", None
        facts = await self.memory.list_facts(user_id)
        if not facts:
            return "Пока ничего о тебе не запомнила. Расскажи о себе, и я учту это в разговорах.", None
        last_page = (len(facts) - 1) // MEMORY_PAGE_SIZE
        page = max(0, min(page, last_page))
        shown = facts[page * MEMORY_PAGE_SIZE : (page + 1) * MEMORY_PAGE_SIZE]
        first = page * MEMORY_PAGE_SIZE + 1
        lines = [f"{first + i}. {fact.content}" for i, fact in enumerate(shown)]
        text = f"👤 Что я о тебе помню ({len(facts)}):\n\n" + "\n".join(lines) + "\n\nНажми номер, чтобы удалить."
        delete_buttons = [
            InlineKeyboardButton(text=f"🗑 {first + i}", callback_data=f"memd:{fact.id.hex}:{page}")
            for i, fact in enumerate(shown)
        ]
        rows = [delete_buttons[i : i + 4] for i in range(0, len(delete_buttons), 4)]
        navigation = []
        if page > 0:
            navigation.append(InlineKeyboardButton(text="←", callback_data=f"mem:{page - 1}"))
        if page < last_page:
            navigation.append(InlineKeyboardButton(text="→", callback_data=f"mem:{page + 1}"))
        if navigation:
            rows.append(navigation)
        rows.append([InlineKeyboardButton(text="Очистить всё", callback_data="memc", style="danger")])
        return text, InlineKeyboardMarkup(inline_keyboard=rows)

    async def _forget_fact(self, callback: CallbackQuery, member: AssistantMember, memory_hex: str, page: int) -> None:
        if self.memory is not None:
            await self.memory.forget(member.telegram_id, uuid.UUID(hex=memory_hex))
        await self._edit(callback, *await self.view(member, page))

    @staticmethod
    def _confirm_forget_all() -> tuple[str, InlineKeyboardMarkup]:
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Да, забыть всё", callback_data="memy", style="danger")],
                [InlineKeyboardButton(text="← Назад", callback_data="mem:0")],
            ]
        )
        return "Забыть всё, что я о тебе знаю, и наш последний разговор? Отменить будет нельзя.", markup

    async def _forget_everything(self, callback: CallbackQuery, user_id: int) -> None:
        if self.memory is not None:
            await self.memory.forget_all(user_id)
        self._forget_conversation(user_id)
        await self._edit(callback, "Готово, всё забыла.", None)
