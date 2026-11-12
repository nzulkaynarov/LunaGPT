"""Family lists: the family adds lines by voice, text or a button and ticks them off; guests never see them."""

from datetime import date, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import Message
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lunagpt.assistant import list_desk, lists
from lunagpt.models import AssistantFamilyList, AssistantFamilyListItem, AssistantMember

KID, MOM = 1, 2


async def _family(db_sessions: async_sessionmaker[AsyncSession]) -> dict[str, AssistantMember]:
    people = {
        "kid": AssistantMember(telegram_id=KID, name="Заурбек", role="kid"),
        "mom": AssistantMember(telegram_id=MOM, name="Камила", role="adult"),
    }
    async with db_sessions() as db:
        db.add_all(people.values())
        await db.commit()
    return people


def _telegram() -> MagicMock:
    return MagicMock(send_message=AsyncMock())


def _callback(data: str) -> MagicMock:
    callback = MagicMock(data=data, answer=AsyncMock())
    callback.message = MagicMock(spec=Message, edit_text=AsyncMock())
    return callback


def _sent(telegram: MagicMock) -> tuple[str, Any]:
    call = telegram.send_message.call_args
    return str(call.args[1]), call.kwargs.get("reply_markup")


def _buttons(markup: Any) -> list[tuple[str, str]]:
    return [(button.text, button.callback_data) for row in markup.inline_keyboard for button in row]


def _hears(monkeypatch: pytest.MonkeyPatch, reply: str | None) -> None:
    monkeypatch.setattr(lists.llm, "complete_json_text", AsyncMock(return_value=reply))


@pytest.mark.parametrize(
    "text",
    [
        "добавь молоко и хлеб в покупки",
        "Добавь в список покупок яйца",
        "надо купить батарейки",
        "запиши в список дел починить кран",
        "покажи список покупок",
        "вычеркни молоко",
        "убери хлеб из списка",
        "добавь шланг в список Дача",
    ],
)
def test_phrases_about_a_list_are_noticed(text: str) -> None:
    assert lists.is_list_request(text)


@pytest.mark.parametrize(
    "text", ["расскажи про покупки в интернете", "привет, как дела?", "напомни завтра купить хлеб"]
)
def test_other_phrases_go_to_the_chat(text: str) -> None:
    assert not lists.is_list_request(text)


def test_items_are_trimmed_and_not_repeated() -> None:
    assert lists.split_items("молоко, Хлеб;\n молоко \n\n яйца 10 шт.") == ["молоко", "Хлеб", "яйца 10 шт"]
    assert lists.list_name("  дача ") == "Дача" and lists.same_item("молоко 2 л", "Молоко")
    assert not lists.same_item("молоко", "мол")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply",
    [None, "не JSON", '{"action": "none"}', '{"action": "add", "list": "Покупки", "items": []}', '{"action": "fly"}'],
)
async def test_an_unclear_request_is_left_to_the_chat(monkeypatch: pytest.MonkeyPatch, reply: str | None) -> None:
    _hears(monkeypatch, reply)
    assert await lists.parse_request("составь список дел на отпуск", ["Покупки", "Дела"], {}) is None


@pytest.mark.asyncio
async def test_spoken_items_land_in_the_list_and_everyone_sees_them(
    db_sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    people = await _family(db_sessions)
    telegram = _telegram()
    desk = list_desk.ListDesk(telegram, db_sessions)
    _hears(monkeypatch, '{"action": "add", "list": "Покупки", "items": ["молоко 2 л", "хлеб"]}')

    assert await desk.request(people["kid"], "добавь молоко два литра и хлеб в покупки") == (
        "✅ Добавила в «Покупки»: молоко 2 л, хлеб"
    )
    text, markup = _sent(telegram)
    assert text.startswith("✅ Добавила в «Покупки»: молоко 2 л, хлеб") and "осталось 2" in text
    assert "молоко 2 л — Заурбек" in text and [b[0] for b in _buttons(markup)][:2] == ["▫️ молоко 2 л", "▫️ хлеб"]

    assert await desk.request(people["mom"], "добавь хлеб в покупки") is not None
    assert "уже есть" not in _sent(telegram)[0]  # the model named two items, only the new one is missing
    _hears(monkeypatch, '{"action": "add", "list": "Покупки", "items": ["Хлеб"]}')
    await desk.request(people["mom"], "добавь хлеб в покупки")
    assert "«Хлеб» уже есть в списке «Покупки»" in _sent(telegram)[0]
    assert "Покупки: молоко 2 л, хлеб" in await desk.planner_text()


@pytest.mark.asyncio
async def test_a_tap_ticks_a_line_and_clearing_removes_it(db_sessions: async_sessionmaker[AsyncSession]) -> None:
    people = await _family(db_sessions)
    telegram = _telegram()
    desk = list_desk.ListDesk(telegram, db_sessions)
    await desk.view(people["mom"])
    overview, markup = _sent(telegram)
    assert overview == list_desk.OVERVIEW and [b[0] for b in _buttons(markup)] == ["🛒 Покупки · 0", "🧹 Дела · 0"]
    shopping = _buttons(markup)[0][1].split(":")[1]

    reply = MagicMock(spec=Message)
    reply.reply_to_message = MagicMock(text=list_desk.ADD_PROMPT.format(name="Покупки"))
    await desk.add_from_reply(people["mom"], reply, "молоко, хлеб\nяйца")
    assert "осталось 3" in _sent(telegram)[0]
    item = _buttons(_sent(telegram)[1])[0][1]

    tap = _callback(item)
    await desk.handle(tap, people["kid"], "flt", [item.split(":")[1]])
    shown = tap.message.edit_text.call_args
    assert "осталось 2, отмечено 1" in shown.args[0] and "✅ молоко" in shown.args[0]
    assert ("🧹 Убрать отмеченное", f"flc:{shopping}") in _buttons(shown.kwargs["reply_markup"])
    assert (await desk.brief_lines(people["mom"], date(2026, 11, 13))) == ["\n🛒 В покупках 2: хлеб, яйца"]
    assert await desk.brief_lines(people["kid"], date(2026, 11, 13)) == []

    clear = _callback(f"flc:{shopping}")
    await desk.handle(clear, people["mom"], "flc", [shopping])
    assert (
        "осталось 2" in clear.message.edit_text.call_args.args[0]
        and "молоко" not in clear.message.edit_text.call_args.args[0]
    )


@pytest.mark.asyncio
async def test_a_named_list_is_created_ticked_by_voice_and_gone_when_empty(
    db_sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    people = await _family(db_sessions)
    telegram = _telegram()
    desk = list_desk.ListDesk(telegram, db_sessions)
    _hears(monkeypatch, '{"action": "add", "list": "дача", "items": ["шланг"]}')
    await desk.request(people["mom"], "добавь шланг в список дача")
    assert "📝 Дача: осталось 1" in _sent(telegram)[0] and await desk.names() == ["Покупки", "Дела", "Дача"]

    _hears(monkeypatch, '{"action": "done", "list": "Дача", "items": ["лопата"]}')
    await desk.request(people["mom"], "вычеркни лопату из списка дача")
    assert "Не нашла в списке «Дача»: лопата" in _sent(telegram)[0]
    _hears(monkeypatch, '{"action": "done", "list": "Дача", "items": ["шланг"]}')
    await desk.request(people["kid"], "вычеркни шланг из списка дача")
    assert "✅ Вычеркнула: шланг" in _sent(telegram)[0] and "осталось 0, отмечено 1" in _sent(telegram)[0]

    async with db_sessions() as db:
        dacha = await db.scalar(select(AssistantFamilyList.id).where(AssistantFamilyList.name == "Дача"))
    clear = _callback(f"flc:{dacha}")
    await desk.handle(clear, people["mom"], "flc", [str(dacha)])
    assert clear.message.edit_text.call_args.args[0] == list_desk.OVERVIEW and await desk.names() == ["Покупки", "Дела"]

    _hears(monkeypatch, '{"action": "show", "list": "Дача", "items": []}')
    await desk.request(people["mom"], "покажи список дача")
    assert _sent(telegram)[0] == "Списка «Дача» у нас нет."


@pytest.mark.asyncio
async def test_a_ticked_line_nobody_cleared_goes_away_by_itself(db_sessions: async_sessionmaker[AsyncSession]) -> None:
    people = await _family(db_sessions)
    desk = list_desk.ListDesk(_telegram(), db_sessions)
    async with db_sessions() as db:
        shopping = await desk._named(db, "Покупки", create=False)
        assert shopping is not None
        await desk._add(db, shopping, ["молоко", "хлеб"], people["mom"].telegram_id)
        long_ago = datetime.now().astimezone() - list_desk.DONE_KEPT - timedelta(hours=1)
        await db.execute(
            update(AssistantFamilyListItem).where(AssistantFamilyListItem.text == "молоко").values(done_at=long_ago)
        )
        await db.commit()
        assert [item.text for item, _ in await desk._items(db, shopping.id)] == ["хлеб"]


@pytest.mark.asyncio
async def test_the_chat_tool_adds_to_a_list_and_reports_what_was_there(
    db_sessions: async_sessionmaker[AsyncSession],
) -> None:
    people = await _family(db_sessions)
    desk = list_desk.ListDesk(_telegram(), db_sessions)
    assert await desk.add_from_chat(people["mom"], "дела", ["Повесить карниз ", "прикрутить полки"]) == (
        "Добавлено в «Дела»: Повесить карниз, прикрутить полки."
    )
    assert await desk.add_from_chat(people["kid"], "Дела", ["повесить карниз", "разобрать сумки"]) == (
        "Добавлено в «Дела»: разобрать сумки. Уже были в списке: повесить карниз."
    )
    assert await desk.add_from_chat(people["mom"], "Дела", ["  "]) == "Нечего добавлять."
    assert "Дела: Повесить карниз, прикрутить полки, разобрать сумки" in await desk.planner_text()
