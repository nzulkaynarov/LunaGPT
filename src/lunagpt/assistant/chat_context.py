"""What the chat model gets with each turn besides the words: the actions it may take, the person's planner, Luna's
facts about herself, the cat's waiting phrases, and the extra lines of the morning brief. Every source is optional:
a failure only leaves its part out."""

import logging
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lunagpt.assistant import access
from lunagpt.assistant.about import about_luna, is_about_question, owner_name
from lunagpt.assistant.calendar_desk import CalendarDesk
from lunagpt.assistant.list_desk import ListDesk
from lunagpt.assistant.lists import quoted
from lunagpt.assistant.llm import (
    LIST_TOOL,
    LISTS_BLOCK,
    LISTS_HINT,
    REMINDER_HINT,
    REMINDER_TOOLS,
    REMINDERS_BLOCK,
    SHOWN_PREFIX,
    Action,
)
from lunagpt.assistant.phrases import PhraseDesk
from lunagpt.assistant.reminder_desk import ReminderDesk
from lunagpt.assistant.school_desk import SchoolDesk
from lunagpt.assistant.stories import StoryDesk
from lunagpt.assistant.waiting import Waiting
from lunagpt.config import settings
from lunagpt.models import AssistantMember

logger = logging.getLogger("lunagpt.assistant.chat_context")

# First, and asking for the first person: placed after the facts, the local model told 2 of 8 stories as
# Luna's own; placed before them, 7 of 8.
STORY_MEMORIES = (
    "Мои воспоминания. Их рассказала семья про настоящую домашнюю кошку Луну, это и есть я. "
    "Когда просят историю обо мне или спрашивают, помню ли я что-то, расскажи подходящую историю отсюда "
    "от первого лица («я залезла…»), а не со стороны, и можешь сказать, кто напомнил:\n{stories}\n\n"
)
MATH_TEXT = re.compile(
    r"\d\s*[-+*/×÷:=]\s*\d|\b(?:задач\w*|уравнени\w*|пример\w*|реши\w*|посчитай|сколько будет)\b", re.I
)


def number_of(args: dict[str, Any]) -> int | None:
    value = args.get("number")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class ChatContext:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        reminders: ReminderDesk,
        lists: ListDesk,
        school: SchoolDesk,
        calendar: CalendarDesk,
        stories: StoryDesk,
        phrases: PhraseDesk,
    ) -> None:
        self._sessions = session_factory
        self._reminders = reminders
        self._lists = lists
        self._school = school
        self._calendar = calendar
        self._stories = stories
        self._phrases = phrases

    async def actions(self, member: AssistantMember) -> tuple[Action, ...]:
        """What the chat model may do for this person: reminders for everyone, the family's lists for family."""
        reminders = self._reminders

        async def set_reminder(args: dict[str, Any]) -> str:
            request = str(args.get("request") or "").strip()
            if not request:
                return "Не сказано, о чём и когда напомнить."
            said = await reminders.create(member, f"напомни {request}")
            return f"{SHOWN_PREFIX} {said}"

        async def schedule_task(args: dict[str, Any]) -> str:
            request = str(args.get("request") or "").strip()
            if not request:
                return "Не сказано, что и когда делать."
            said = await reminders.create(member, f"напомни {request}", kind="do")
            return f"{SHOWN_PREFIX} {said}. В это время ты сама выполнишь задание и пришлёшь результат."

        async def move_reminder(args: dict[str, Any]) -> str:
            number = number_of(args)
            return (
                "Нет номера напоминания."
                if number is None
                else await reminders.move(member, number, str(args.get("when") or ""))
            )

        async def cancel_reminder(args: dict[str, Any]) -> str:
            number = number_of(args)
            return "Нет номера напоминания." if number is None else await reminders.cancel(member, number)

        runs = (set_reminder, schedule_task, move_reminder, cancel_reminder)
        actions = [
            Action(schema, run, REMINDERS_BLOCK, REMINDER_HINT)
            for schema, run in zip(REMINDER_TOOLS, runs, strict=True)
        ]
        if member.role in access.FAMILY_ROLES:
            lists = self._lists
            try:
                family, personal = await lists.names(member)
            except Exception:
                logger.warning("could not load the family lists", exc_info=True)
                return tuple(actions)

            async def add_to_list(args: dict[str, Any]) -> str:
                items = args.get("items")
                if not isinstance(items, list):
                    return "Нет пунктов."
                result: str = await lists.add_from_chat(
                    member, str(args.get("list") or ""), [str(i) for i in items], args.get("personal") is True
                )
                return result

            names = {"names": quoted(family), "personal": quoted(personal)}
            actions.append(Action(LIST_TOOL, add_to_list, LISTS_BLOCK.format(**names), LISTS_HINT.format(**names)))
        return tuple(actions)

    async def about(self, member: AssistantMember, text: str) -> str:
        """Luna's facts about herself, only for a question about her; a failure just leaves them out."""
        if not is_about_question(text):
            return ""
        owner = None
        if member.role != "guest":
            try:
                async with self._sessions() as db:
                    owner = await owner_name(db)
            except Exception:
                logger.warning("could not look up the owner", exc_info=True)
        facts: str = about_luna(member.role, owner)
        if member.role in access.FAMILY_ROLES:
            try:
                memories = await self._stories.memories()
            except Exception:
                logger.warning("could not load the cat stories", exc_info=True)
                memories = ""
            if memories:
                facts = STORY_MEMORIES.format(stories=memories) + facts
        return facts

    async def waiting(self, member: AssistantMember, text: str, speak: bool, photo: bool) -> Waiting:
        """The cat's phrases for this answer; the family's own ones are never shown to guests."""
        kind = "math" if MATH_TEXT.search(text) else "photo" if photo else "voice" if speak else "chat"
        family: list[str] = []
        if member.role in access.FAMILY_ROLES:
            try:
                family = await self._phrases.family_phrases() + await self._stories.phrases()
            except Exception:
                logger.warning("could not load family phrases", exc_info=True)
        now = datetime.now(UTC) + timedelta(hours=settings.luna_utc_offset_hours)
        return Waiting(kind, now.replace(tzinfo=None), family)

    async def planner(self, member: AssistantMember) -> str:
        """Timetable, tasks, calendar, reminders and the family's lists for the model; a failure only loses this
        context."""
        parts: list[str] = []
        for source in (self._school.planner_block, self._calendar.planner_text, self._reminders.planner_text):
            try:
                parts.append(await source(member.telegram_id))
            except Exception:
                logger.warning("could not load planner context", exc_info=True)
        if member.role in access.FAMILY_ROLES:
            try:
                parts.append(await self._lists.planner_text(member))
            except Exception:
                logger.warning("could not load the family lists", exc_info=True)
        return "\n\n".join(part for part in parts if part)

    async def brief_extra(self, user_id: int, today: date) -> list[str]:
        """Calendar, school, lists and a cat story for the morning brief."""
        lines: list[str] = []
        for source in (self._calendar.brief_lines, self._school.brief_lines):
            try:
                lines += await source(user_id, today)
            except Exception:
                logger.warning("could not load brief lines", exc_info=True)
        try:
            async with self._sessions() as db:
                member = await access.get_member(db, user_id)
            if member is not None:
                lines += await self._lists.brief_lines(member, today)
                lines += await self._stories.brief_lines(member, today)
        except Exception:
            logger.warning("could not load the lists or a cat story for the brief", exc_info=True)
        return lines
