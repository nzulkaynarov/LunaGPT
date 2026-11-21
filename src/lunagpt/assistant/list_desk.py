"""Lists in Telegram: the family's shared ones and each person's own. Lines are added by voice, text or a
button and ticked off with a tap.

Every family member, children included, sees and changes the family's lists; a personal list is seen only by
its owner. Guests never see the family's lists. Every screen checks who may see the list it shows."""

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from aiogram import Bot
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lunagpt.assistant import access, lists
from lunagpt.assistant.telegram import (
    CANCEL,
    DANGER,
    Previews,
    Rich,
    edit_screen,
    esc,
    is_reply_to_prompt,
    remove_menu,
    send_long,
    send_prompt,
)
from lunagpt.models import AssistantFamilyList, AssistantFamilyListItem, AssistantMember

logger = logging.getLogger("lunagpt.assistant.lists")

LIST_PREFIXES = {"flo", "flt", "fla", "flc", "flb", "flm", "flq", "fln", "flnq", "fls", "flx", "fld", "fldy"}
LIST_CANCEL = "flq"  # "✖ Отмена" under "what to add": the list, removed when it was asked, comes back
NEW_LIST = "fln"  # "➕ Новый список" on the overview
NEW_LIST_CANCEL = "flnq"  # "✖ Отмена" under "how to name it": all the lists, removed when it was asked, come back
WHOSE = "fls"  # "🏠 Для всей семьи" or "👤 Только мне" for the name just given
WHOSE_CANCEL = "flx"  # "✖ Отмена" under that question: it turns back into all the lists
DROP = "fld"  # "🗑 Удалить список" on an empty list someone named: it asks first
DROP_YES = "fldy"
FOR_FAMILY, FOR_ME = "f", "p"
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
NOTHING_TO_ADD = "Не поняла, что добавить."
TOO_MANY_LISTS = f"Списков уже {MAX_LISTS}. Сначала очисти какой-нибудь из них."
LIST_FULL = f"В списке уже {MAX_OPEN_ITEMS} пунктов. Сначала отметь или убери лишнее."
LIST_GONE = "Этого списка уже нет."
ITEM_GONE = "Этого пункта в списке уже нет."
BTN_NEW_LIST = "➕ Новый список"
NEW_LIST_PROMPT = "📝 Как назвать новый список? Например: «Дача» или «Аптека»."
NAME_EMPTY = "Не поняла название."
NAME_TOO_LONG = f"Название длинное: нужно не больше {lists.MAX_NAME_CHARS} знаков."
NAME_TAKEN = "Так уже называется список «{name}». Придумай другое название."
# A usual word for a standing list is not a free name: phrases with it land in that list.
ALIAS_TAKEN = (
    "«{name}» — это уже список «{meant}»: всё, что просят добавить в {said}, я кладу туда. Придумай другое название."
)
MINE_TAKEN = f"«{{name}}» — это и есть твой список «{LOCK} {lists.MY_LIST}», он уже есть. Придумай другое название."
WHOSE_QUESTION = "📝 Список «{name}»: для кого он? 🏠 Семейный видит и меняет вся семья, 👤 личный — только ты."
BTN_FOR_FAMILY = "🏠 Для всей семьи"
BTN_FOR_ME = "👤 Только мне"
NAME_LOST = f"Это уже неактуально: нажми «{BTN_NEW_LIST}» ещё раз."
LIST_MADE = "✅ Создала список «{name}». Добавь, что в нём будет."
LIST_WAS_THERE = "Список «{name}» уже есть, вот он."
BTN_DROP = "🗑 Удалить список"
CONFIRM_DROP = "🗑 Удалить пустой список «{name}»? Он пропадёт у всех, кто его видит."
CONFIRM_DROP_MINE = "🗑 Удалить пустой список «{name}»?"
LIST_DROPPED = "🗑 Удалила список «{name}»."
NOT_EMPTY = "В списке уже есть пункты, удалять не стала."


def icon(target: AssistantFamilyList) -> str:
    return LOCK if target.owner_id is not None else ICONS.get(target.name, "📝")


def label(target: AssistantFamilyList) -> str:
    """How a list is named on screen and in prompts: a personal one carries the lock."""
    return f"{LOCK} {target.name}" if target.owner_id is not None else target.name


def standing(target: AssistantFamilyList) -> bool:
    """«Покупки», «Дела по дому» and a person's «Мои дела» are always there; other lists were named by someone."""
    return target.name in (lists.DEFAULT_LISTS if target.owner_id is None else (lists.MY_LIST,))


def short(text: str, limit: int = MAX_BUTTON_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


@dataclass
class Added:
    """What became of the lines someone asked to add."""

    added: list[str] = field(default_factory=list)
    already: list[str] = field(default_factory=list)  # open in the list already
    no_room: list[str] = field(default_factory=list)  # the list was full


class ListDesk:
    def __init__(self, bot: Bot, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._bot = bot
        self._sessions = sessions
        self._new_names: Previews[str] = Previews()  # a new list's name waits for "🏠" or "👤" under its token

    @staticmethod
    def is_add_reply(message: Message) -> bool:
        found: bool = is_reply_to_prompt(message, ADD_PROMPT_HEAD)
        return found

    @staticmethod
    def is_name_reply(message: Message) -> bool:
        found: bool = is_reply_to_prompt(message, NEW_LIST_PROMPT)
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

    async def _add(self, db: AsyncSession, target: AssistantFamilyList, items: list[str], author: int) -> Added:
        """Add the lines that are not already open in the list, while there is room."""
        seen = {item.text.lower() for item, _ in await self._items(db, target.id) if item.done_at is None}
        room = MAX_OPEN_ITEMS - len(seen)
        result = Added()
        for item in items:
            if item.lower() in seen:
                result.already.append(item)
            elif len(result.added) >= room:
                result.no_room.append(item)
            else:
                result.added.append(item)
                seen.add(item.lower())
        db.add_all(AssistantFamilyListItem(list_id=target.id, text=item, added_by=author) for item in result.added)
        await db.commit()
        return result

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
        rows.append([InlineKeyboardButton(text=BTN_NEW_LIST, callback_data=NEW_LIST)])
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
        count = f"осталось {left}" + (f", отмечено {ticked}" if ticked else "")
        personal = target.owner_id is not None
        parts = [f"<h3>{esc(icon(target))} {esc(target.name)}</h3>", f"<p>{count}</p>"]
        if items:
            entries = [
                f'<li><input type="checkbox"{" checked" if item.done_at else ""}>{esc(item.text)}'
                + (f" — <i>{esc(author)}</i>" if author and not item.done_at and not personal else "")
                + "</li>"
                for item, author in shown
            ]
            parts.append("<ul>" + "".join(entries) + "</ul>")
            if len(items) > MAX_SHOWN:
                parts.append(f"<p>… и ещё {len(items) - MAX_SHOWN}</p>")
        else:
            parts.append("<p>Пока пусто. Нажми «➕ Добавить» или скажи: «добавь … в список».</p>")
        buttons = [
            InlineKeyboardButton(
                text=("✅ " if item.done_at else "▫️ ") + short(item.text), callback_data=f"flt:{item.id}:{list_id}"
            )
            for item, _ in shown
        ]
        rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
        actions = [InlineKeyboardButton(text="➕ Добавить", callback_data=f"fla:{list_id}")]
        if ticked:
            actions.append(InlineKeyboardButton(text="🧹 Убрать отмеченное", callback_data=f"flc:{list_id}"))
        rows.append(actions)
        if not items and not standing(target):  # a typo or a wrong «🏠/👤» is not there for good
            rows.append([InlineKeyboardButton(text=BTN_DROP, callback_data=f"{DROP}:{list_id}", style=DANGER)])
        rows.append([InlineKeyboardButton(text="⬅️ Все списки", callback_data="flb")])
        return Rich("".join(parts)), InlineKeyboardMarkup(inline_keyboard=rows)

    async def view(self, member: AssistantMember) -> None:
        text, markup = await self._overview(member)
        await self._bot.send_message(member.telegram_id, text, reply_markup=markup)

    async def _list_screen(
        self, member: AssistantMember, list_id: int, note: str = ""
    ) -> tuple[str, InlineKeyboardMarkup] | None:
        """The list with a note on top; None when the list is gone or not this person's to see."""
        view = await self._list_view(member, list_id)
        if view is None:
            return None
        return Rich((f"<p>{esc(note)}</p>" if note else "") + view[0]), view[1]

    async def _send_list(self, member: AssistantMember, list_id: int, note: str = "") -> None:
        screen = await self._list_screen(member, list_id, note)
        if screen is None:
            await self._send_gone(member)
            return
        await send_long(self._bot, member.telegram_id, *screen)

    async def _send_gone(self, member: AssistantMember) -> None:
        """A list that went away leads to all the lists, never to a dead end."""
        await self._send_overview(member, LIST_GONE)

    async def _send_overview(self, member: AssistantMember, note: str) -> None:
        text, markup = await self._overview(member)
        await self._bot.send_message(member.telegram_id, f"{note}\n\n{text}", reply_markup=markup)

    async def _ask_add(self, member: AssistantMember, target: AssistantFamilyList, problem: str = "") -> None:
        prompt = ADD_PROMPT.format(name=label(target))
        await send_prompt(self._bot, member.telegram_id, prompt, f"{LIST_CANCEL}:{target.id}", problem)

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
        name, mine = lists.meant_list(asked.name, asked.personal)
        async with self._sessions() as db:
            target = await self._named(db, member, name, mine, create=asked.action == "add")
            if target is None:
                note = TOO_MANY_LISTS if asked.action == "add" else f"Списка «{name}» нет."
                await self._send_overview(member, note)
                return note
            if asked.action == "add":
                note = self._added_note(label(target), await self._add(db, target, asked.items, member.telegram_id))
            elif asked.action == "done":
                note = await self._tick_named(db, target, asked.items)
            else:
                note = ""
        await self._send_list(member, target.id, note)
        return note or f"Показала список «{label(target)}»."

    @staticmethod
    def _added_note(name: str, result: Added) -> str:
        """What was added, what was there already and what did not fit, each said as it is."""
        parts = []
        if result.added:
            parts.append(f"✅ Добавила в «{name}»: {', '.join(result.added)}")
        if len(result.already) == 1 and not result.added:
            parts.append(f"«{result.already[0]}» уже есть в списке «{name}»")
        elif result.already:
            parts.append(f"Уже есть{'' if result.added else f' в «{name}»'}: {', '.join(result.already)}")
        if result.no_room:
            parts.append(f"Не поместилось: {', '.join(result.no_room)}")
        note = ". ".join(parts)
        return f"{note}. {LIST_FULL}" if result.no_room else note

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
        unnamed = lists.MY_LIST if personal else lists.CHORES
        name, personal = lists.meant_list(lists.list_name(name) or unnamed, personal)
        if not wanted:
            return "Нечего добавлять."
        async with self._sessions() as db:
            target = await self._named(db, member, name, personal, create=True)
            if target is None:
                return f"Не добавлено: списков уже {MAX_LISTS}, новый список не создать."
            done = await self._add(db, target, wanted, member.telegram_id)
        where = label(target)
        result = f"Добавлено в «{where}»: {', '.join(done.added)}." if done.added else f"Ничего нового в «{where}»."
        if done.already:
            result += f" Уже были в списке: {', '.join(done.already)}."
        if done.no_room:
            result += f" Не добавлено, список полон ({MAX_OPEN_ITEMS} пунктов): {', '.join(done.no_room)}."
        return result

    async def add_from_reply(self, member: AssistantMember, message: Message, text: str) -> None:
        """The answer to "what to add?": the list is the one named in the prompt."""
        prompt = (message.reply_to_message.text if message.reply_to_message else "") or ""
        named = PROMPT_NAME.search(prompt)
        shown = named.group(1) if named else ""
        personal = shown.startswith((LOCK + " ", OLD_LOCK + " "))
        items = lists.split_items(text)
        name = shown.removeprefix(LOCK + " ").removeprefix(OLD_LOCK + " ")
        async with self._sessions() as db:
            target = await self._named(db, member, name, personal, create=False) if name else None
            added = await self._add(db, target, items, member.telegram_id) if target and items else Added()
        if target is None:
            await self._send_gone(member)
        elif not items:
            await self._ask_add(member, target, NOTHING_TO_ADD)
        else:
            await self._send_list(member, target.id, self._added_note(label(target), added))

    # ---------- a new list by buttons ----------

    async def _taken(self, member: AssistantMember, name: str) -> str:
        """Why this name cannot be a new list, said as it is; '' when it can. A usual word for a standing list
        («продукты», «дела») is taken by that list, since phrases with it land there; «мои дела» and «мой список»
        mean the person's own «Мои дела», taken once it is there."""
        family, personal = await self.names(member)
        key = name.casefold()
        found = next((one for one in family if one.casefold() == key), None)
        if found is not None:
            return NAME_TAKEN.format(name=found)
        found = next((one for one in personal if one.casefold() == key), None)
        if found is not None:
            return NAME_TAKEN.format(name=f"{LOCK} {found}")
        meant, mine = lists.meant_list(name, False)
        if mine:
            there = any(one.casefold() == lists.MY_LIST.casefold() for one in personal)
            return MINE_TAKEN.format(name=name) if there else ""
        return ALIAS_TAKEN.format(name=name, meant=meant, said=name.casefold()) if meant != name else ""

    async def name_from_reply(self, member: AssistantMember, text: str) -> None:
        """The answer to "how to name it": a wrong name gets the question again, a good one asks whose list it is;
        «мои дела» is always the person's own, so it opens at once."""
        # Quotes inside a name would cut it short where a prompt quotes it back, so they become plain ones.
        typed = " ".join(text.replace("«", '"').replace("»", '"').split()).strip(lists.NAME_TRIM)
        name = lists.list_name(typed)
        problem = ""
        if not name:
            problem = NAME_EMPTY
        elif len(typed) > lists.MAX_NAME_CHARS:
            problem = NAME_TOO_LONG
        else:
            problem = await self._taken(member, name)
        if problem:
            await send_prompt(self._bot, member.telegram_id, NEW_LIST_PROMPT, NEW_LIST_CANCEL, problem)
            return
        if lists.meant_list(name, False)[1]:  # the same as the «👤 Мои дела» button: no question whose it is
            async with self._sessions() as db:
                mine = await self._named(db, member, lists.MY_LIST, personal=True, create=True)
            if mine is None:
                await self._send_overview(member, TOO_MANY_LISTS)
            else:
                await self._send_list(member, mine.id, LIST_MADE.format(name=label(mine)))
            return
        token = self._new_names.keep(member.telegram_id, name)
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text=BTN_FOR_FAMILY, callback_data=f"{WHOSE}:{token}:{FOR_FAMILY}"),
                    InlineKeyboardButton(text=BTN_FOR_ME, callback_data=f"{WHOSE}:{token}:{FOR_ME}"),
                ],
                [InlineKeyboardButton(text=CANCEL, callback_data=f"{WHOSE_CANCEL}:{token}")],
            ]
        )
        await self._bot.send_message(member.telegram_id, WHOSE_QUESTION.format(name=name), reply_markup=markup)

    async def _new_list(self, callback: CallbackQuery, member: AssistantMember, prefix: str, args: list[str]) -> None:
        if prefix == NEW_LIST:
            family, personal = await self.names(member)
            if len(family) >= MAX_LISTS and len(personal) >= MAX_LISTS:
                text, markup = await self._overview(member)
                await self._edit(callback, f"{TOO_MANY_LISTS}\n\n{text}", markup)
                return
            await send_prompt(self._bot, member.telegram_id, NEW_LIST_PROMPT, NEW_LIST_CANCEL)
            await remove_menu(callback)  # the new list itself comes in its place
            return
        if prefix == NEW_LIST_CANCEL:
            await remove_menu(callback)
            await self.view(member)
            return
        name = self._new_names.take(member.telegram_id, args[0] if args else None)
        if prefix == WHOSE_CANCEL or name is None:
            text, markup = await self._overview(member)
            await self._edit(callback, text if prefix == WHOSE_CANCEL else f"{NAME_LOST}\n\n{text}", markup)
            return
        mine = args[1:] == [FOR_ME]
        async with self._sessions() as db:
            there = await self._named(db, member, name, mine, create=False)
            target = there or await self._named(db, member, name, mine, create=True)
        if target is None:
            text, markup = await self._overview(member)
            await self._edit(callback, f"{TOO_MANY_LISTS}\n\n{text}", markup)
            return
        note = (LIST_WAS_THERE if there else LIST_MADE).format(name=label(target))
        screen = await self._list_screen(member, target.id, note)
        if screen is None:  # gone already: all the lists, never a dead end
            text, markup = await self._overview(member)
            screen = (f"{LIST_GONE}\n\n{text}", markup)
        await self._edit(callback, *screen)

    # ---------- buttons ----------

    async def handle(self, callback: CallbackQuery, member: AssistantMember, prefix: str, args: list[str]) -> None:
        if prefix in (NEW_LIST, NEW_LIST_CANCEL, WHOSE, WHOSE_CANCEL):
            await self._new_list(callback, member, prefix, args)
            return
        if prefix in (DROP, DROP_YES):
            await self._drop(callback, member, prefix, int(args[0]))
            return
        if prefix == "flb":
            await self._edit(callback, *await self._overview(member))
            return
        if prefix == LIST_CANCEL:
            await remove_menu(callback)
            await self._send_list(member, int(args[0]))
            return
        note = ""
        if prefix == "flm":
            async with self._sessions() as db:
                mine = await self._named(db, member, lists.MY_LIST, personal=True, create=True)
            list_id: int | None = mine.id if mine is not None else None
        elif prefix == "flt":
            list_id = await self._toggle(member, int(args[0]))
            if list_id is None and len(args) > 1:  # the line was cleared on another screen: its list redraws
                list_id, note = int(args[1]), ITEM_GONE
        else:
            list_id = int(args[0])
        view = await self._list_screen(member, list_id, note) if list_id is not None else None
        if list_id is None or view is None:
            # A list that went away leads to all the lists, never to a dead end.
            text, markup = await self._overview(member)
            await self._edit(callback, f"{LIST_GONE}\n\n{text}", markup)
            return
        if prefix == "fla":
            async with self._sessions() as db:
                target = await self._visible(db, member, list_id)
            if target is not None:
                await self._ask_add(member, target)
                await remove_menu(callback)
            return
        if prefix == "flc" and await self._clear(list_id):
            await self._edit(callback, *await self._overview(member))
            return
        if prefix == "flc":
            view = await self._list_view(member, list_id)
        if view is not None:
            await self._edit(callback, *view)

    async def _drop(self, callback: CallbackQuery, member: AssistantMember, prefix: str, list_id: int) -> None:
        """«🗑 Удалить список» asks first, then removes the list while it is still empty and all the lists come back.
        A list that got lines meanwhile stays and says why; one that is gone leads to all the lists."""
        async with self._sessions() as db:
            target = await self._visible(db, member, list_id)
            empty = target is not None and not standing(target) and not await self._items(db, list_id)
            name = label(target) if target is not None else ""
            if empty and target is not None and prefix == DROP_YES:
                await db.delete(target)
                await db.commit()
        if target is None or (empty and prefix == DROP_YES):
            text, markup = await self._overview(member)
            note = LIST_DROPPED.format(name=name) if target is not None else LIST_GONE
            await self._edit(callback, f"{note}\n\n{text}", markup)
            return
        if not empty:
            screen = await self._list_screen(member, list_id, "" if standing(target) else NOT_EMPTY)
            if screen is not None:
                await self._edit(callback, *screen)
            return
        question = CONFIRM_DROP_MINE if target.owner_id is not None else CONFIRM_DROP
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Да, удалить", callback_data=f"{DROP_YES}:{list_id}", style=DANGER)],
                [InlineKeyboardButton(text="← Назад", callback_data=f"flo:{list_id}")],
            ]
        )
        await self._edit(callback, question.format(name=name), markup)

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
            gone = target is not None and not standing(target) and not left
            if gone:
                await db.delete(target)
            await db.commit()
            return gone

    @staticmethod
    async def _edit(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None) -> None:
        if isinstance(callback.message, Message):
            await edit_screen(callback.message, text, markup)
