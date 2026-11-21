"""Children's waiting phrases: suggested by a child, approved or turned down by a parent, then shown."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telegram_fakes import prompt_cancel

from lunagpt.assistant import phrases, stories
from lunagpt.models import AssistantMember

KID, MOM = 1, 2


async def _family(db_sessions: async_sessionmaker[AsyncSession]) -> tuple[AssistantMember, AssistantMember]:
    kid = AssistantMember(telegram_id=KID, name="Заурбек", role="kid")
    mom = AssistantMember(telegram_id=MOM, name="Камила", role="adult")
    async with db_sessions() as db:
        db.add_all([kid, mom])
        await db.commit()
    return kid, mom


def _callback(data: str) -> MagicMock:
    callback = MagicMock(data=data, answer=AsyncMock())
    callback.message = MagicMock(spec=Message, edit_text=AsyncMock())
    return callback


def _texts(telegram: MagicMock, chat_id: int) -> list[str]:
    return [str(c.args[1]) for c in telegram.send_message.call_args_list if c.args[0] == chat_id]


@pytest.mark.asyncio
async def test_kid_phrase_waits_for_a_parent_and_then_shows(db_sessions: async_sessionmaker[AsyncSession]) -> None:
    kid, mom = await _family(db_sessions)
    telegram = MagicMock(send_message=AsyncMock())
    desk = phrases.PhraseDesk(telegram, db_sessions)

    await desk.suggest(kid, "🐾 Ловлю ответ за хвост…")
    assert _texts(telegram, KID)[-1] == phrases.SENT
    review = telegram.send_message.call_args_list[0]
    assert review.args[0] == MOM and "Заурбек" in review.args[1]
    assert await desk.family_phrases() == []  # nothing is shown before a parent says yes

    approve = _callback("cpy:1")
    await desk.handle(approve, mom, "cpy", ["1"])
    assert await desk.family_phrases() == ["🐾 Ловлю ответ за хвост…\n— автор: Заурбек"]
    assert "одобрили" in _texts(telegram, KID)[-1]

    again = _callback("cpn:1")
    await desk.handle(again, mom, "cpn", ["1"])  # a second parent pressing late changes nothing
    assert again.message.edit_text.call_args.args[0] == phrases.ALREADY_DECIDED

    await desk.handle(_callback("cpx:1"), mom, "cpx", ["1"])
    assert await desk.family_phrases() == []


@pytest.mark.asyncio
async def test_turned_down_bad_length_and_too_many(db_sessions: async_sessionmaker[AsyncSession]) -> None:
    kid, mom = await _family(db_sessions)
    telegram = MagicMock(send_message=AsyncMock())
    desk = phrases.PhraseDesk(telegram, db_sessions)

    await desk.suggest(kid, "ой")
    fresh = _texts(telegram, KID)[-1]
    assert fresh == f"{phrases.TOO_SHORT}\n{phrases.PROMPT}"
    assert prompt_cancel(telegram.send_message.call_args.kwargs["reply_markup"]) == phrases.PHRASE_CANCEL
    answer = MagicMock(spec=Message)
    answer.reply_to_message = MagicMock(text=fresh, from_user=MagicMock(is_bot=True))
    assert desk.is_reply(answer)  # the fresh prompt is answered like the first one
    await desk.suggest(kid, "первая\nвторая строка")
    assert _texts(telegram, KID)[-1] == f"{phrases.TOO_SHORT}\n{phrases.PROMPT}"

    await desk.suggest(kid, "😼 Шиплю на трудный вопрос…")
    await desk.handle(_callback("cpn:1"), mom, "cpn", ["1"])
    assert "не добавили" in _texts(telegram, KID)[-1] and await desk.family_phrases() == []

    for n in range(phrases.MAX_PENDING):
        await desk.suggest(kid, f"🐾 Фраза номер {n}")
    await desk.suggest(kid, "🐾 Ещё одна фраза")
    assert _texts(telegram, KID)[-1] == phrases.TOO_MANY


@pytest.mark.asyncio
async def test_a_parents_own_phrase_needs_no_approval(db_sessions: async_sessionmaker[AsyncSession]) -> None:
    _, mom = await _family(db_sessions)
    telegram = MagicMock(send_message=AsyncMock())
    desk = phrases.PhraseDesk(telegram, db_sessions)
    await desk.suggest(mom, "🐈 Мама-кошка думает…")
    assert await desk.family_phrases() == ["🐈 Мама-кошка думает…\n— автор: Камила"]


@pytest.mark.asyncio
async def test_a_sent_phrase_brings_back_the_stories_screen(db_sessions: async_sessionmaker[AsyncSession]) -> None:
    kid, _ = await _family(db_sessions)
    telegram = MagicMock(send_message=AsyncMock())
    stories_desk = stories.StoryDesk(telegram, db_sessions)
    desk = phrases.PhraseDesk(telegram, db_sessions, screen=stories_desk.view)
    await desk.suggest(kid, "🐾 Ловлю ответ за хвост…")
    told = telegram.send_message.call_args
    assert told.args[0] == KID and told.args[1].startswith(f"{phrases.SENT}\n\n🐈 Истории про Луну")
    buttons = [b.callback_data for row in told.kwargs["reply_markup"].inline_keyboard for b in row]
    assert stories.PHRASE_ASK in buttons and "mem:0" in buttons


@pytest.mark.asyncio
async def test_too_many_waiting_phrases_are_said_before_anything_is_typed(
    db_sessions: async_sessionmaker[AsyncSession],
) -> None:
    kid, _ = await _family(db_sessions)
    telegram = MagicMock(send_message=AsyncMock())
    desk = phrases.PhraseDesk(telegram, db_sessions)
    tap = _callback(stories.PHRASE_ASK)
    tap.message.delete = AsyncMock()
    await desk.start(kid, tap)
    assert telegram.send_message.call_args.args == (KID, phrases.PROMPT)
    tap.message.delete.assert_awaited_once()  # the stories screen made way for the prompt

    for n in range(phrases.MAX_PENDING):
        await desk.suggest(kid, f"🐾 Фраза номер {n}")
    telegram.send_message.reset_mock()
    again = _callback(stories.PHRASE_ASK)
    again.message.delete = AsyncMock()
    await desk.start(kid, again)
    shown = again.message.edit_text.call_args
    assert shown.args[0] == phrases.TOO_MANY
    assert [b.callback_data for row in shown.kwargs["reply_markup"].inline_keyboard for b in row] == ["csi"]
    again.message.delete.assert_not_awaited()
    telegram.send_message.assert_not_awaited()  # no prompt to type into in vain

    await desk.start(kid)  # the typed menu button: the same answer as a new message
    said = telegram.send_message.call_args
    assert said.args == (KID, phrases.TOO_MANY)
    assert [b.callback_data for row in said.kwargs["reply_markup"].inline_keyboard for b in row] == ["csi"]
