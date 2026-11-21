"""Stories about the cat Luna: told by the family, approved by a parent, remembered by Luna."""

from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telegram_fakes import prompt_cancel

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


def _screen(callback: MagicMock) -> tuple[str, list[str]]:
    """What a pressed button redrew its message into: the text and every button's data."""
    shown = callback.message.edit_text.call_args
    return str(shown.args[0]), [b.callback_data for row in shown.kwargs["reply_markup"].inline_keyboard for b in row]


@pytest.mark.asyncio
async def test_kid_story_is_approved_and_then_remembered(db_sessions: async_sessionmaker[AsyncSession]) -> None:
    people = await _family(db_sessions)
    telegram = _telegram()
    desk = stories.StoryDesk(telegram, db_sessions)

    await desk.tell(people["kid"], STORY)
    assert _last(telegram, KID).startswith(f"{stories.SENT}\n\n🐈 Истории про Луну")  # the screen comes back
    told = telegram.send_message.call_args
    buttons = [b.callback_data for row in told.kwargs["reply_markup"].inline_keyboard for b in row]
    assert "csa" in buttons and "mem:0" in buttons
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
    assert _last(telegram, KID) == f"{stories.TOO_SHORT}\n{stories.PROMPT}"
    await desk.tell(people["kid"], "мяу " * stories.MAX_LENGTH)
    assert _last(telegram, KID) == f"{stories.TOO_LONG}\n{stories.PROMPT}"
    assert prompt_cancel(telegram.send_message.call_args.kwargs["reply_markup"]) == stories.STORY_CANCEL
    for n in range(stories.MAX_PENDING):
        await desk.tell(people["kid"], f"История номер {n} про Луну")
    await desk.tell(people["kid"], "Ещё одна история про Луну")
    assert _last(telegram, KID) == stories.TOO_MANY
    back = telegram.send_message.call_args.kwargs["reply_markup"]
    assert [b.callback_data for row in back.inline_keyboard for b in row] == ["csi"]

    tell = _callback("csa")  # the button says so before anything is typed
    tell.message.delete = AsyncMock()
    sent_before = telegram.send_message.await_count
    await desk.handle(tell, people["kid"], "csa", [])
    assert _screen(tell) == (stories.TOO_MANY, ["csi"])
    assert telegram.send_message.await_count == sent_before and tell.message.delete.await_count == 0

    await desk.tell(people["mom"], "Луна каждое утро будит всех в шесть")
    assert _last(telegram, MOM).startswith(f"{stories.ADDED}\n\n🐈 Истории про Луну: 1.")
    assert len(await desk.approved()) == 1
    adult_tell = _callback("csa")
    adult_tell.message.delete = AsyncMock()
    await desk.handle(adult_tell, people["mom"], "csa", [])  # adults' stories never wait
    assert _last(telegram, MOM) == stories.PROMPT


@pytest.mark.asyncio
async def test_list_brief_and_removal(db_sessions: async_sessionmaker[AsyncSession]) -> None:
    people = await _family(db_sessions)
    telegram = _telegram()
    desk = stories.StoryDesk(telegram, db_sessions)
    empty = _callback("csl:0")
    await desk.handle(empty, people["kid"], "csl", ["0"])
    assert _screen(empty) == (stories.NONE_YET, ["csa", "csi"])  # the first story to tell, or back

    await desk.tell(people["mom"], STORY)
    listing = _callback("csl:0")
    await desk.handle(listing, people["mom"], "csl", ["0"])
    text, buttons = _screen(listing)
    assert STORY in text and buttons == ["csx:1:0", "csi"]

    brief_day = next(date.fromordinal(d) for d in range(739000, 739010) if d % stories.BRIEF_EVERY_DAYS == 0)
    assert "🐈 А помнишь?" in (await desk.brief_lines(people["kid"], brief_day))[0]
    assert await desk.brief_lines(people["guest"], brief_day) == []
    assert await desk.brief_lines(people["kid"], date.fromordinal(brief_day.toordinal() + 1)) == []

    await desk.handle(_callback("csx:1"), people["mom"], "csx", ["1"])
    assert await desk.approved() == []


@pytest.mark.parametrize("text", ["расскажи смешную историю", "расскажи историю про себя", "ты помнишь как"])
def test_story_questions_bring_in_the_memories(text: str) -> None:
    assert about.is_about_question(text)


def _answer_to(prompt: str) -> MagicMock:
    answer = MagicMock(spec=Message)
    answer.reply_to_message = MagicMock(text=prompt, from_user=MagicMock(is_bot=True))
    return answer


@pytest.mark.asyncio
async def test_a_too_short_story_brings_a_fresh_prompt_that_still_works(
    db_sessions: async_sessionmaker[AsyncSession],
) -> None:
    people = await _family(db_sessions)
    telegram = _telegram()
    desk = stories.StoryDesk(telegram, db_sessions)
    await desk.tell(people["kid"], "мяу")
    fresh = _last(telegram, KID)
    assert fresh.startswith(stories.TOO_SHORT) and desk.is_reply(_answer_to(fresh))
    await desk.tell(people["kid"], "", photo_file_id="file-1")  # a photo with no caption: the caption is missing
    fresh = _last(telegram, KID)
    assert fresh == f"{stories.NO_CAPTION}\n{stories.PROMPT}" and desk.is_reply(_answer_to(fresh))
    telegram.send_photo.assert_not_awaited()
    await desk.tell(people["kid"], STORY)
    assert _last(telegram, KID).startswith(stories.SENT)


@pytest.mark.asyncio
async def test_cancelling_the_story_prompt_brings_the_stories_back(
    db_sessions: async_sessionmaker[AsyncSession],
) -> None:
    people = await _family(db_sessions)
    telegram = _telegram()
    desk = stories.StoryDesk(telegram, db_sessions)
    menu = _callback("csa")
    menu.message.delete = AsyncMock()
    await desk.handle(menu, people["kid"], "csa", [])
    menu.message.delete.assert_awaited_once()  # the stories screen made way for the prompt
    asked = telegram.send_message.call_args
    assert asked.args == (KID, stories.PROMPT)
    cancel = _callback(prompt_cancel(asked.kwargs["reply_markup"]))
    cancel.message.delete = AsyncMock()
    await desk.handle(cancel, people["kid"], stories.STORY_CANCEL, [])
    cancel.message.delete.assert_awaited_once()
    shown = telegram.send_message.call_args
    assert shown.args[1].startswith("🐈 Истории про Луну")
    buttons = [b.callback_data for row in shown.kwargs["reply_markup"].inline_keyboard for b in row]
    assert "csa" in buttons and stories.PHRASE_ASK in buttons


@pytest.mark.asyncio
async def test_story_pages_lead_on_back_and_survive_a_removal(db_sessions: async_sessionmaker[AsyncSession]) -> None:
    people = await _family(db_sessions)
    desk = stories.StoryDesk(_telegram(), db_sessions)
    for n in range(stories.PAGE_SIZE + 1):
        await desk.tell(people["mom"], f"История номер {n} про Луну")
    few = _callback("csl:0")
    await desk.handle(few, people["kid"], "csl", ["0"])
    assert _screen(few)[1] == ["csl:1", "csi"]  # a child removes nothing, but can always read on or go back
    last = _callback("csl:1")
    await desk.handle(last, people["kid"], "csl", ["1"])
    text, buttons = _screen(last)
    assert text.endswith("11. История номер 0 про Луну — Камила") and buttons == ["csl:0", "csi"]

    removal = _callback("csx:1:1")  # an adult removes the only story of the last page
    await desk.handle(removal, people["mom"], "csx", ["1", "1"])
    text, buttons = _screen(removal)
    assert text.startswith("🗑 Убрано: «История номер 0 про Луну»") and "1. История номер 10 про Луну" in text
    assert "csl:1" not in buttons and buttons[-1] == "csi" and len(await desk.approved()) == stories.PAGE_SIZE
    again = _callback("csx:1:1")  # the same button pressed twice finds the list, not a dead end
    await desk.handle(again, people["mom"], "csx", ["1", "1"])
    assert _screen(again)[0].startswith("📖 Истории про Луну")
