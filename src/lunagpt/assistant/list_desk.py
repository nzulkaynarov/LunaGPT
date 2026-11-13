"""Lists in Telegram: the family's shared ones and each person's own. Lines are added by voice, text or a
button and ticked off with a tap.

Every family member, children included, sees and changes the family's lists; a personal list is seen only by
its owner. Guests never see the family's lists. Every screen checks who may see the list it shows."""

import logging
import re
import uuid
from datetime import date, datetime, timedelta

from aiogram import Bot
from aiogram.types import CallbackQuery, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lunagpt.assistant import access, lists
from lunagpt.assistant.telegram import is_reply_to_prompt, remove_menu
from lunagpt.models import AssistantFamilyList, AssistantFamilyListItem, AssistantMember

logger = logging.getLogger("lunagpt.assistant.lists")

LIST_PREFIXES = {"flo", "flt", "fla", "flc", "flb", "flm"}
MAX_LISTS = 12  # for the family, and again for each person
MAX_OPEN_ITEMS = 100
MAX_SHOWN = 40
MAX_IN_PROMPT = 40
MAX_BUTTON_CHARS = 24
DONE_KEPT = timedelta(days=2)  # a ticked line nobody cleared goes away by itself
ICONS = {lists.SHOPPING: "🛒", lists.CHORES: "🧹"}
LOCK = "👤"  # only mine; the family's things carry 🏠 in the menus
OLD_LOCK = "🔒"  # prompts sent before the change still carry it
OVERVIEW = (
    "📋 Списки. 🏠 Семейные видит и меняет вся семья, 👤 личные — только ты.\n"
    "Скажи или напиши: «добавь молоко и хлеб в покупки», «вычеркни молоко», «добавь шланг в список Дача», "
    "«добавь в мой список записаться к врачу»."
)
ADD_PROMPT_HEAD = "➕ Что добавить в список"
ADD_PROMPT = ADD_PROMPT_HEAD + " «{name}»? Можно несколько: через запятую или с новой строки.\nОтветь на это сообщение."
PROMPT_NAME = re.compile(r"«([^»]+)»")
NOTHING_TO_ADD = "Не поняла, что добавить. Напиши пункты через запятую."
TOO_MANY_LISTS = f"Списков уже {MAX_LISTS}. Сначала очисти какой-нибудь из них."
LIST_FULL = f"В списке уже {MAX_OPEN_ITEMS} пунктов. Сначала отметь или убери лишнее."
LIST_GONE = "Этого списка уже нет."


def icon(target: AssistantFamilyList) -> str:
    return LOCK if target.owner_id is not None else ICONS.get(target.name, "📝")


def label(target: AssistantFamilyList) -> str:
    """How a list is named on screen and in prompts: a personal one carries the lock."""
    return f"{LOCK} {target.name}" if target.owner_id is not None else target.name


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

    @staticmethod
    def _family(member: AssistantMember) -> bool:
        return member.role in access.FAMILY_ROLES

    async def _all(self, db: AsyncSession, member: AssistantMember) -> list[AssistantFamilyList]:
        """The lists this person sees: the family's (the two standing ones are made on first use), then their own."""
        family = self._family(member)
        if family:
            names = set(
                await db.scalars(select(AssistantFamilyList.name).where(AssistantFamilyList.owner_id.is_(None)))
            )
            missing = [name for name in lists.DEFAULT_LISTS if name not in names]
            if missing:
                db.add_all(AssistantFamilyList(name=name) for name in missing)
                await db.commit()
        mine = AssistantFamilyList.owner_id == member.telegram_id
        seen = or_(AssistantFamilyList.owner_id.is_(None), mine) if family else mine
        rows = await db.scalars(
            select(AssistantFamilyList)
            .where(seen)
            .order_by(AssistantFamilyList.owner_id.is_not(None), AssistantFamilyList.id)
        )
        return list(rows)

    async def _named(
        self, db: AsyncSession, member: AssistantMember, name: str, personal: bool, create: bool
    ) -> AssistantFamilyList | None:
        personal = personal or not self._family(member)
        owner = member.telegram_id if personal else None
        every = [one for one in await self._all(db, member) if one.owner_id == owner]
        for one in every:
            if one.name.lower() == name.lower():
                return one
        if not create or len(every) >= MAX_LISTS:
            return None
        made = AssistantFamilyList(name=name, owner_id=owner)
        db.add(made)
        await db.commit()
        return made

    async def _visible(self, db: AsyncSession, member: AssistantMember, list_id: int) -> AssistantFamilyList | None:
        target = await db.get(AssistantFamilyList, list_id)
        if target is None:
            return None
        if target.owner_id is None:
            return target if self._family(member) else None
        return target if target.owner_id == member.telegram_id else None

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

    async def names(self, member: AssistantMember) -> tuple[list[str], list[str]]:
        """The family's list names and this person's own."""
        async with self._sessions() as db:
            every = await self._all(db, member)
        return [one.name for one in every if one.owner_id is None], [one.name for one in every if one.owner_id]

    async def planner_text(self, member: AssistantMember) -> str:
        """The open lines of every list this person sees, for the model, as data."""
        async with self._sessions() as db:
            seen = [one.id for one in await self._all(db, member)]
            rows = await db.execute(
                select(AssistantFamilyList, AssistantFamilyListItem.text)
                .join(AssistantFamilyListItem, AssistantFamilyListItem.list_id == AssistantFamilyList.id)
                .where(AssistantFamilyListItem.done_at.is_(None), AssistantFamilyList.id.in_(seen))
                .order_by(AssistantFamilyList.owner_id.is_not(None), AssistantFamilyList.id, AssistantFamilyListItem.id)
                .limit(MAX_IN_PROMPT)
            )
            by_list: dict[str, list[str]] = {}
            for target, text in rows:
                kind = "личный" if target.owner_id is not None else "общий"
                by_list.setdefault(f"{target.name} ({kind})", []).append(text)
        if not by_list:
            return ""
        lines = [f"- {name}: {', '.join(items)}" for name, items in by_list.items()]
        return "Списки (что ещё не куплено и не сделано):\n" + "\n".join(lines)

    async def brief_lines(self, member: AssistantMember, _day: date) -> list[str]:
        """Adults see what is left to buy for the family; everyone sees what is left in their own lists."""
        found: list[str] = []
        async with self._sessions() as db:
            for target in await self._all(db, member):
                personal = target.owner_id is not None
                if not personal and (target.name != lists.SHOPPING or member.role not in access.ADULT_ROLES):
                    continue
                left = [item.text for item, _ in await self._items(db, target.id) if item.done_at is None]
                if not left:
                    continue
                shown = ", ".join(left[:5]) + (f" и ещё {len(left) - 5}" if len(left) > 5 else "")
                where = label(target) if personal else f"{icon(target)} В покупках {len(left)}"
                found.append(f"\n{where}: {shown}")
        return found

    # ---------- screens ----------

    async def _overview(self, member: AssistantMember) -> tuple[str, InlineKeyboardMarkup]:
        async with self._sessions() as db:
            every = await self._all(db, member)
            counts = await self._open_counts(db)
        rows = [
            [
                InlineKeyboardButton(
                    text=f"{icon(one)} {one.name} · {counts.get(one.id, 0)}", callback_data=f"flo:{one.id}"
                )
            ]
            for one in every
        ]
        if not any(one.owner_id for one in every):
            rows.append([InlineKeyboardButton(text=f"{LOCK} {lists.MY_LIST}", callback_data="flm")])
        return OVERVIEW, InlineKeyboardMarkup(inline_keyboard=rows)

    async def _list_view(self, member: AssistantMember, list_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
        async with self._sessions() as db:
            target = await self._visible(db, member, list_id)
            if target is None:
                return None
            items = await self._items(db, list_id)
        shown = items[:MAX_SHOWN]
        left = sum(1 for item, _ in items if item.done_at is None)
        ticked = len(items) - left
        head = f"{icon(target)} {target.name}: осталось {left}" + (f", отмечено {ticked}" if ticked else "")
        personal = target.owner_id is not None
        lines = [
            ("✅ " if item.done_at else "▫️ ")
            + item.text
            + (f" — {author}" if author and not item.done_at and not personal else "")
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
        text, markup = await self._overview(member)
        await self._bot.send_message(member.telegram_id, text, reply_markup=markup)

    async def _send_list(self, member: AssistantMember, list_id: int, note: str = "") -> None:
        view = await self._list_view(member, list_id)
        if view is None:
            await self._bot.send_message(member.telegram_id, LIST_GONE)
            return
        await self._bot.send_message(
            member.telegram_id, (note + "\n\n" if note else "") + view[0], reply_markup=view[1]
        )

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
        family, personal = await self.names(member)
        asked = await lists.parse_request(text, family, personal, metadata)
        if asked is None:
            return None
        async with self._sessions() as db:
            target = await self._named(db, member, asked.name, asked.personal, create=asked.action == "add")
            if target is None:
                note = TOO_MANY_LISTS if asked.action == "add" else f"Списка «{asked.name}» нет."
                await self._bot.send_message(member.telegram_id, note)
                return note
            if asked.action == "add":
                added = await self._add(db, target, asked.items, member.telegram_id)
                note = self._added_note(label(target), asked.items, added)
            elif asked.action == "done":
                note = await self._tick_named(db, target, asked.items)
            else:
                note = ""
        await self._send_list(member, target.id, note)
        return note or f"Показала список «{label(target)}»."

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
            return f"Не нашла в списке «{label(target)}»: {', '.join(wanted)}"
        return f"✅ Вычеркнула: {', '.join(ticked)}"

    async def add_from_chat(self, member: AssistantMember, name: str, items: list[str], personal: bool = False) -> str:
        """What the chat model adds with its tool; the answer tells the model what happened, as data."""
        wanted = lists.clean_items(items)
        name = lists.list_name(name) or (lists.MY_LIST if personal else lists.CHORES)
        if not wanted:
            return "Нечего добавлять."
        async with self._sessions() as db:
            target = await self._named(db, member, name, personal, create=True)
            if target is None:
                return f"Не добавлено: списков уже {MAX_LISTS}, новый список не создать."
            added = await self._add(db, target, wanted, member.telegram_id)
        already = [item for item in wanted if item not in added]
        where = label(target)
        result = f"Добавлено в «{where}»: {', '.join(added)}." if added else f"Ничего нового в «{where}»."
        return result + (f" Уже были в списке: {', '.join(already)}." if already else "")

    async def add_from_reply(self, member: AssistantMember, message: Message, text: str) -> None:
        """The answer to "what to add?": the list is the one named in the prompt."""
        prompt = (message.reply_to_message.text if message.reply_to_message else "") or ""
        named = PROMPT_NAME.search(prompt)
        items = lists.split_items(text)
        if named is None or not items:
            await self._bot.send_message(member.telegram_id, NOTHING_TO_ADD)
            return
        shown = named.group(1)
        personal = shown.startswith((LOCK + " ", OLD_LOCK + " "))
        async with self._sessions() as db:
            name = shown.removeprefix(LOCK + " ").removeprefix(OLD_LOCK + " ")
            target = await self._named(db, member, name, personal, create=False)
            if target is None:
                await self._bot.send_message(member.telegram_id, LIST_GONE)
                return
            added = await self._add(db, target, items, member.telegram_id)
        await self._send_list(member, target.id, self._added_note(label(target), items, added))

    # ---------- buttons ----------

    async def handle(self, callback: CallbackQuery, member: AssistantMember, prefix: str, args: list[str]) -> None:
        if prefix == "flb":
            await self._edit(callback, *await self._overview(member))
            return
        if prefix == "flm":
            async with self._sessions() as db:
                mine = await self._named(db, member, lists.MY_LIST, personal=True, create=True)
            list_id: int | None = mine.id if mine is not None else None
        elif prefix == "flt":
            list_id = await self._toggle(member, int(args[0]))
        else:
            list_id = int(args[0])
        view = await self._list_view(member, list_id) if list_id is not None else None
        if list_id is None or view is None:
            await self._edit(callback, LIST_GONE, None)
            return
        if prefix == "fla":
            async with self._sessions() as db:
                target = await self._visible(db, member, list_id)
            if target is not None:
                await self._bot.send_message(
                    member.telegram_id, ADD_PROMPT.format(name=label(target)), reply_markup=ForceReply()
                )
                await remove_menu(callback)
            return
        if prefix == "flc" and await self._clear(list_id):
            await self._edit(callback, *await self._overview(member))
            return
        if prefix == "flc":
            view = await self._list_view(member, list_id)
        if view is not None:
            await self._edit(callback, *view)

    async def _toggle(self, member: AssistantMember, item_id: int) -> int | None:
        async with self._sessions() as db:
            item = await db.get(AssistantFamilyListItem, item_id)
            if item is None or await self._visible(db, member, item.list_id) is None:
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
            standing = lists.DEFAULT_LISTS if target is None or target.owner_id is None else (lists.MY_LIST,)
            gone = target is not None and target.name not in standing and not left
            if gone:
                await db.delete(target)
            await db.commit()
            return gone

    @staticmethod
    async def _edit(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None) -> None:
        if isinstance(callback.message, Message):
            await callback.message.edit_text(text, reply_markup=markup)
