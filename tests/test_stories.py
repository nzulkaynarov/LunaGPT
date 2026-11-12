"""Stories about the cat Luna: told by the family, approved by a parent, remembered by Luna."""

from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lunagpt.assistant import about, stories
from lunagpt.models import AssistantMember

KID, MOM, GUEST = 1, 2, 3
STORY = "Луна залезла в коробку из-под пиццы и проспала там весь вечер"


async def _family(db_sessions: async_sessionmaker[AsyncSession]) -> dict[str, AssistantMember]:
    people = {
        "kid": AssistantMember(telegram_id=KID, name="Заурбек", role="kid"),
        "mom": AssistantMember(telegram_id=MOM, name="Камила", role="adult"),
        "guest": AssistantMember(telegram_id=GUEST, name="Гость", role="guest"),
    }
    async with db_sessions() as db:
        db.add_all(people.values())
        await db.commit()
    return people


def _callback(data: str, photo: bool = False) -> MagicMock:
    callback = MagicMock(data=data, answer=AsyncMock())
    callback.message = MagicMock(spec=Message, edit_text=AsyncMock(), edit_caption=AsyncMock(), photo=photo)
    return callback


def _telegram() -> MagicMock:
    return MagicMock(send_message=AsyncMock(), send_photo=AsyncMock())


def _last(telegram: MagicMock, chat_id: int) -> str:
    return [str(c.args[1]) for c in telegram.send_message.call_args_list if c.args[0] == chat_id][-1]


@pytest.mark.asyncio
async def test_kid_story_is_approved_and_then_remembered(db_sessions: async_sessionmaker[AsyncSession]) -> None:
    people = await _family(db_sessions)
    telegram = _telegram()
    desk = stories.StoryDesk(telegram, db_sessions)

    await desk.tell(people["kid"], STORY)
    assert _last(telegram, KID) == stories.SENT
    review = [c for c in telegram.send_message.call_args_list if c.args[0] == MOM][0]
    assert "Заурбек рассказывает про Луну" in review.args[1]
    assert await desk.memories() == "" and await desk.phrases() == []

    await desk.handle(_callback("csy:1"), people["mom"], "csy", ["1"])
    assert "Заурбек" in await desk.memories() and STORY in await desk.memories()
    assert (await desk.phrases())[0].startswith("💭 Вспоминаю историю про себя") and "одобрили" in _last(telegram, KID)

    late = _callback("csn:1")
    await desk.handle(late, people["mom"], "csn", ["1"])
    assert late.message.edit_text.call_args.args[0] == stories.ALREADY_DECIDED


@pytest.mark.asyncio
async def test_photo_story_goes_to_parents_as_a_photo(db_sessions: async_sessionmaker[AsyncSession]) -> None:
    people = await _family(db_sessions)
    telegram = _telegram()
    desk = stories.StoryDesk(telegram, db_sessions)
    await desk.tell(people["kid"], "Луна спит на клавиатуре папы", photo_file_id="file-1")
    assert telegram.send_photo.call_args.args[:2] == (MOM, "file-1")
    turned_down = _callback("csn:1", photo=True)
    await desk.handle(turned_down, people["mom"], "csn", ["1"])
    assert "Не добавлено" in turned_down.message.edit_caption.call_args.kwargs["caption"]
    assert "не добавили" in _last(telegram, KID)


@pytest.mark.asyncio
async def test_limits_and_parents_stories(db_sessions: async_sessionmaker[AsyncSession]) -> None:
    people = await _family(db_sessions)
    telegram = _telegram()
    desk = stories.StoryDesk(telegram, db_sessions)
    await desk.tell(people["kid"], "мяу")
    assert _last(telegram, KID) == stories.TOO_SHORT
    for n in range(stories.MAX_PENDING):
        await desk.tell(people["kid"], f"История номер {n} про Луну")
    await desk.tell(people["kid"], "Ещё одна история про Луну")
    assert _last(telegram, KID) == stories.TOO_MANY

    await desk.tell(people["mom"], "Луна каждое утро будит всех в шесть")
    assert _last(telegram, MOM) == stories.ADDED and len(await desk.approved()) == 1


@pytest.mark.asyncio
async def test_list_brief_and_removal(db_sessions: async_sessionmaker[AsyncSession]) -> None:
    people = await _family(db_sessions)
    telegram = _telegram()
    desk = stories.StoryDesk(telegram, db_sessions)
    await desk.handle(_callback("csl:0"), people["kid"], "csl", ["0"])
    assert _last(telegram, KID) == stories.NONE_YET

    await desk.tell(people["mom"], STORY)
    await desk.handle(_callback("csl:0"), people["mom"], "csl", ["0"])
    listing = telegram.send_message.call_args
    assert STORY in listing.args[1] and listing.kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "csx:1"

    brief_day = next(date.fromordinal(d) for d in range(739000, 739010) if d % stories.BRIEF_EVERY_DAYS == 0)
    assert "🐈 А помнишь?" in (await desk.brief_lines(people["kid"], brief_day))[0]
    assert await desk.brief_lines(people["guest"], brief_day) == []
    assert await desk.brief_lines(people["kid"], date.fromordinal(brief_day.toordinal() + 1)) == []

    await desk.handle(_callback("csx:1"), people["mom"], "csx", ["1"])
    assert await desk.approved() == []


@pytest.mark.parametrize("text", ["расскажи смешную историю", "расскажи историю про себя", "ты помнишь как"])
def test_story_questions_bring_in_the_memories(text: str) -> None:
    assert about.is_about_question(text)
