"""Family lists in Telegram: the family adds lines by voice, text or a button and ticks them off with a tap.

Every family member, children included, sees and changes the same lists; guests never see them."""

import logging
import re
import uuid
from datetime import date, datetime, timedelta

from aiogram import Bot
from aiogram.types import CallbackQuery, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lunagpt.assistant import access, lists
from lunagpt.assistant.telegram import is_reply_to_prompt
from lunagpt.models import AssistantFamilyList, AssistantFamilyListItem, AssistantMember

logger = logging.getLogger("lunagpt.assistant.lists")

LIST_PREFIXES = {"flo", "flt", "fla", "flc", "flb"}
MAX_LISTS = 12
MAX_OPEN_ITEMS = 100
MAX_SHOWN = 40
MAX_IN_PROMPT = 40
MAX_BUTTON_CHARS = 24
DONE_KEPT = timedelta(days=2)  # a ticked line nobody cleared goes away by itself
ICONS = {lists.SHOPPING: "🛒", lists.CHORES: "🧹"}
OVERVIEW = (
    "📋 Семейные списки. Их видит и меняет вся семья.\n"
    "Скажи или напиши: «добавь молоко и хлеб в покупки», «вычеркни молоко», «добавь шланг в список Дача»."
)
ADD_PROMPT_HEAD = "➕ Что добавить в список"
ADD_PROMPT = ADD_PROMPT_HEAD + " «{name}»? Можно несколько: через запятую или с новой строки.\nОтветь на это сообщение."
PROMPT_NAME = re.compile(r"«([^»]+)»")
NOTHING_TO_ADD = "Не поняла, что добавить. Напиши пункты через запятую."
TOO_MANY_LISTS = f"Списков уже {MAX_LISTS}. Сначала очисти какой-нибудь из них."
LIST_FULL = f"В списке уже {MAX_OPEN_ITEMS} пунктов. Сначала отметь или убери лишнее."
LIST_GONE = "Этого списка уже нет."


def icon(name: str) -> str:
    return ICONS.get(name, "📝")


def short(text: str, limit: int = MAX_BUTTON_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class ListDesk:
    def __init__(self, bot: Bot, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._bot = bot
        self._sessions = sessions

    @staticmethod
    def is_add_reply(message: Message) -> bool:
        found: bool = is_reply_to_prompt(message, ADD_PROMPT_HEAD)
        return found

    # ---------- storage ----------

    async def _all(self, db: AsyncSession) -> list[AssistantFamilyList]:
        """Every list, the two standing ones first; they are created on first use."""
        found = list(await db.scalars(select(AssistantFamilyList).order_by(AssistantFamilyList.id)))
        missing = [name for name in lists.DEFAULT_LISTS if name not in {one.name for one in found}]
        if missing:
            db.add_all(AssistantFamilyList(name=name) for name in missing)
            await db.commit()
            found = list(await db.scalars(select(AssistantFamilyList).order_by(AssistantFamilyList.id)))
        return found

    async def _named(self, db: AsyncSession, name: str, create: bool) -> AssistantFamilyList | None:
        every = await self._all(db)
        for one in every:
            if one.name.lower() == name.lower():
                return one
        if not create or len(every) >= MAX_LISTS:
            return None
        made = AssistantFamilyList(name=name)
        db.add(made)
        await db.commit()
        return made

    @staticmethod
    async def _items(db: AsyncSession, list_id: int) -> list[tuple[AssistantFamilyListItem, str | None]]:
        """Open lines first, then ticked ones, each with the name of who added it."""
        await db.execute(
            delete(AssistantFamilyListItem).where(
                AssistantFamilyListItem.list_id == list_id,
                AssistantFamilyListItem.done_at < datetime.now().astimezone() - DONE_KEPT,
            )
        )
        await db.commit()
        rows = await db.execute(
            select(AssistantFamilyListItem, AssistantMember.name)
            .outerjoin(AssistantMember, AssistantMember.telegram_id == AssistantFamilyListItem.added_by)
            .where(AssistantFamilyListItem.list_id == list_id)
            .order_by(AssistantFamilyListItem.done_at.is_not(None), AssistantFamilyListItem.id)
        )
        return [(item, name) for item, name in rows]

    async def _open_counts(self, db: AsyncSession) -> dict[int, int]:
        rows = await db.execute(
            select(AssistantFamilyListItem.list_id, func.count())
            .where(AssistantFamilyListItem.done_at.is_(None))
            .group_by(AssistantFamilyListItem.list_id)
        )
        return {list_id: count for list_id, count in rows}

    async def _add(self, db: AsyncSession, target: AssistantFamilyList, items: list[str], author: int) -> list[str]:
        """Add the lines that are not already open in the list; return the ones added."""
        open_now = [item.text for item, _ in await self._items(db, target.id) if item.done_at is None]
        room = MAX_OPEN_ITEMS - len(open_now)
        fresh = [item for item in items if item.lower() not in {text.lower() for text in open_now}][: max(room, 0)]
        db.add_all(AssistantFamilyListItem(list_id=target.id, text=item, added_by=author) for item in fresh)
        await db.commit()
        return fresh

    # ---------- what Luna knows ----------

    async def names(self) -> list[str]:
        async with self._sessions() as db:
            return [one.name for one in await self._all(db)]

    async def planner_text(self) -> str:
        """The open lines of every list for the model, as data."""
        async with self._sessions() as db:
            rows = await db.execute(
                select(AssistantFamilyList.name, AssistantFamilyListItem.text)
                .join(AssistantFamilyListItem, AssistantFamilyListItem.list_id == AssistantFamilyList.id)
                .where(AssistantFamilyListItem.done_at.is_(None))
                .order_by(AssistantFamilyList.id, AssistantFamilyListItem.id)
                .limit(MAX_IN_PROMPT)
            )
            by_list: dict[str, list[str]] = {}
            for name, text in rows:
                by_list.setdefault(name, []).append(text)
        if not by_list:
            return ""
        lines = [f"- {name}: {', '.join(items)}" for name, items in by_list.items()]
        return "Общие списки семьи (что ещё не куплено и не сделано):\n" + "\n".join(lines)

    async def brief_lines(self, member: AssistantMember, _day: date) -> list[str]:
        """Adults see what is left to buy in the morning brief."""
        if member.role not in access.ADULT_ROLES:
            return []
        async with self._sessions() as db:
            shopping = await self._named(db, lists.SHOPPING, create=False)
            if shopping is None:
                return []
            left = [item.text for item, _ in await self._items(db, shopping.id) if item.done_at is None]
        if not left:
            return []
        more = f" и ещё {len(left) - 5}" if len(left) > 5 else ""
        return [f"\n🛒 В покупках {len(left)}: {', '.join(left[:5])}{more}"]

    # ---------- screens ----------

    async def _overview(self) -> tuple[str, InlineKeyboardMarkup]:
        async with self._sessions() as db:
            every = await self._all(db)
            counts = await self._open_counts(db)
        rows = [
            [
                InlineKeyboardButton(
                    text=f"{icon(one.name)} {one.name} · {counts.get(one.id, 0)}", callback_data=f"flo:{one.id}"
                )
            ]
            for one in every
        ]
        return OVERVIEW, InlineKeyboardMarkup(inline_keyboard=rows)

    async def _list_view(self, list_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
        async with self._sessions() as db:
            target = await db.get(AssistantFamilyList, list_id)
            if target is None:
                return None
            items = await self._items(db, list_id)
        shown = items[:MAX_SHOWN]
        left = sum(1 for item, _ in items if item.done_at is None)
        ticked = len(items) - left
        head = f"{icon(target.name)} {target.name}: осталось {left}" + (f", отмечено {ticked}" if ticked else "")
        lines = [
            ("✅ " if item.done_at else "▫️ ") + item.text + (f" — {author}" if author and not item.done_at else "")
            for item, author in shown
        ]
        if not items:
            lines = ["Пока пусто. Нажми «➕ Добавить» или скажи: «добавь … в список»."]
        elif len(items) > MAX_SHOWN:
            lines.append(f"… и ещё {len(items) - MAX_SHOWN}")
        buttons = [
            InlineKeyboardButton(
                text=("✅ " if item.done_at else "▫️ ") + short(item.text), callback_data=f"flt:{item.id}"
            )
            for item, _ in shown
        ]
        rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
        actions = [InlineKeyboardButton(text="➕ Добавить", callback_data=f"fla:{list_id}")]
        if ticked:
            actions.append(InlineKeyboardButton(text="🧹 Убрать отмеченное", callback_data=f"flc:{list_id}"))
        rows += [actions, [InlineKeyboardButton(text="⬅️ Все списки", callback_data="flb")]]
        return head + "\n\n" + "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)

    async def view(self, member: AssistantMember) -> None:
        text, markup = await self._overview()
        await self._bot.send_message(member.telegram_id, text, reply_markup=markup)

    async def _send_list(self, chat_id: int, list_id: int, note: str = "") -> None:
        view = await self._list_view(list_id)
        if view is None:
            await self._bot.send_message(chat_id, LIST_GONE)
            return
        await self._bot.send_message(chat_id, (note + "\n\n" if note else "") + view[0], reply_markup=view[1])

    # ---------- by voice or text ----------

    async def request(self, member: AssistantMember, text: str) -> str | None:
        """Act on "добавь молоко в покупки" and return what the person was told; None when the message is not
        about a list after all."""
        metadata: dict[str, object] = {
            "trace_id": str(uuid.uuid4()),
            "user_id": f"member:{member.telegram_id}",
            "session_id": f"luna:{member.telegram_id}",
            "source": "luna_assistant",
            "tags": ["client:luna-assistant", "feature:lists", f"role:{member.role}"],
        }
        asked = await lists.parse_request(text, await self.names(), metadata)
        if asked is None:
            return None
        async with self._sessions() as db:
            target = await self._named(db, asked.name, create=asked.action == "add")
            if target is None:
                note = TOO_MANY_LISTS if asked.action == "add" else f"Списка «{asked.name}» у нас нет."
                await self._bot.send_message(member.telegram_id, note)
                return note
            if asked.action == "add":
                added = await self._add(db, target, asked.items, member.telegram_id)
                note = self._added_note(target.name, asked.items, added)
            elif asked.action == "done":
                note = await self._tick_named(db, target, asked.items)
            else:
                note = ""
        await self._send_list(member.telegram_id, target.id, note)
        return note or f"Показала список «{target.name}»."

    @staticmethod
    def _added_note(name: str, asked: list[str], added: list[str]) -> str:
        if not added:
            return LIST_FULL if len(asked) > 1 else f"«{asked[0]}» уже есть в списке «{name}»."
        return f"✅ Добавила в «{name}»: {', '.join(added)}"

    @staticmethod
    async def _tick_named(db: AsyncSession, target: AssistantFamilyList, wanted: list[str]) -> str:
        rows = await db.scalars(
            select(AssistantFamilyListItem).where(
                AssistantFamilyListItem.list_id == target.id, AssistantFamilyListItem.done_at.is_(None)
            )
        )
        ticked: list[str] = []
        for item in rows:
            if any(lists.same_item(item.text, want) for want in wanted):
                item.done_at = datetime.now().astimezone()
                ticked.append(item.text)
        await db.commit()
        if not ticked:
            return f"Не нашла в списке «{target.name}»: {', '.join(wanted)}"
        return f"✅ Вычеркнула: {', '.join(ticked)}"

    async def add_from_chat(self, member: AssistantMember, name: str, items: list[str]) -> str:
        """What the chat model adds with its tool; the answer tells the model what happened, as data."""
        wanted = lists.clean_items(items)
        name = lists.list_name(name) or lists.CHORES
        if not wanted:
            return "Нечего добавлять."
        async with self._sessions() as db:
            target = await self._named(db, name, create=True)
            if target is None:
                return f"Не добавлено: списков уже {MAX_LISTS}, новый список не создать."
            added = await self._add(db, target, wanted, member.telegram_id)
        already = [item for item in wanted if item not in added]
        result = f"Добавлено в «{target.name}»: {', '.join(added)}." if added else f"Ничего нового в «{target.name}»."
        return result + (f" Уже были в списке: {', '.join(already)}." if already else "")

    async def add_from_reply(self, member: AssistantMember, message: Message, text: str) -> None:
        """The answer to "what to add?": the list is the one named in the prompt."""
        prompt = (message.reply_to_message.text if message.reply_to_message else "") or ""
        named = PROMPT_NAME.search(prompt)
        items = lists.split_items(text)
        if named is None or not items:
            await self._bot.send_message(member.telegram_id, NOTHING_TO_ADD)
            return
        async with self._sessions() as db:
            target = await self._named(db, named.group(1), create=False)
            if target is None:
                await self._bot.send_message(member.telegram_id, LIST_GONE)
                return
            added = await self._add(db, target, items, member.telegram_id)
        await self._send_list(member.telegram_id, target.id, self._added_note(target.name, items, added))

    # ---------- buttons ----------

    async def handle(self, callback: CallbackQuery, member: AssistantMember, prefix: str, args: list[str]) -> None:
        if prefix == "flb":
            await self._edit(callback, *await self._overview())
            return
        list_id = await self._toggle(int(args[0])) if prefix == "flt" else int(args[0])
        if list_id is None:
            await self._edit(callback, LIST_GONE, None)
            return
        if prefix == "fla":
            async with self._sessions() as db:
                target = await db.get(AssistantFamilyList, list_id)
            if target is not None:
                await self._bot.send_message(
                    member.telegram_id, ADD_PROMPT.format(name=target.name), reply_markup=ForceReply()
                )
            return
        if prefix == "flc" and await self._clear(list_id):
            await self._edit(callback, *await self._overview())
            return
        view = await self._list_view(list_id)
        if view is None:
            await self._edit(callback, LIST_GONE, None)
            return
        await self._edit(callback, *view)

    async def _toggle(self, item_id: int) -> int | None:
        async with self._sessions() as db:
            item = await db.get(AssistantFamilyListItem, item_id)
            if item is None:
                return None
            item.done_at = None if item.done_at else datetime.now().astimezone()
            list_id: int = item.list_id
            await db.commit()
            return list_id

    async def _clear(self, list_id: int) -> bool:
        """Drop the ticked lines; a list the family named itself goes away once it is empty. True when it did."""
        async with self._sessions() as db:
            await db.execute(
                delete(AssistantFamilyListItem).where(
                    AssistantFamilyListItem.list_id == list_id, AssistantFamilyListItem.done_at.is_not(None)
                )
            )
            target = await db.get(AssistantFamilyList, list_id)
            left = await db.scalar(
                select(func.count())
                .select_from(AssistantFamilyListItem)
                .where(AssistantFamilyListItem.list_id == list_id)
            )
            gone = target is not None and target.name not in lists.DEFAULT_LISTS and not left
            if gone:
                await db.delete(target)
            await db.commit()
            return gone

    @staticmethod
    async def _edit(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None) -> None:
        if isinstance(callback.message, Message):
            await callback.message.edit_text(text, reply_markup=markup)
