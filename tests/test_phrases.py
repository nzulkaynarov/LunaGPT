"""Children's waiting phrases: suggested by a child, approved or turned down by a parent, then shown."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lunagpt.assistant import phrases
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
    assert _texts(telegram, KID)[-1] == phrases.TOO_SHORT
    await desk.suggest(kid, "первая\nвторая строка")
    assert _texts(telegram, KID)[-1] == phrases.TOO_SHORT

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
