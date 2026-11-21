"""Family lists: the family adds lines by voice, text or a button and ticks them off; guests never see them."""

from datetime import date, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import Message
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telegram_fakes import edited, fake_bot, last_sent, prompt_cancel

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
    return fake_bot()


def _callback(data: str) -> MagicMock:
    callback = MagicMock(data=data, answer=AsyncMock())
    callback.message = MagicMock(spec=Message, edit_text=AsyncMock(), delete=AsyncMock())
    return callback


def _sent(telegram: MagicMock) -> tuple[str, Any]:
    return last_sent(telegram)


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
        "добавь в мой список записаться к врачу",
        "добавь в мои дела позвонить в банк",
        "добавь помыть окна в дела",
        "в дела по дому добавь прикрутить полку",
        "добавь дела по дому: помыть окна",
        "покажи дела по дому",
        "убери окна из дел",
    ],
)
def test_phrases_about_a_list_are_noticed(text: str) -> None:
    assert lists.is_list_request(text)


@pytest.mark.parametrize(
    "text",
    [
        "расскажи про покупки в интернете",
        "привет, как дела?",
        "как дела в школе?",
        "напомни завтра купить хлеб",
    ],
)
def test_other_phrases_go_to_the_chat(text: str) -> None:
    assert not lists.is_list_request(text)


@pytest.mark.parametrize(
    ("said", "personal", "meant"),
    [
        ("Дела", False, (lists.CHORES, False)),
        ("дела  по дому", False, (lists.CHORES, False)),
        ("Домашние дела", False, (lists.CHORES, False)),
        ("Список покупок", False, (lists.SHOPPING, False)),
        ("Покупки", False, (lists.SHOPPING, False)),
        ("Мои дела", False, (lists.MY_LIST, True)),
        ("Мой список", False, (lists.MY_LIST, True)),
        ("Дела", True, (lists.MY_LIST, True)),
        ("Дача", False, ("Дача", False)),
        ("Дача", True, ("Дача", True)),
        ("Делами", False, ("Делами", False)),
    ],
)
def test_usual_words_name_the_standing_lists(said: str, personal: bool, meant: tuple[str, bool]) -> None:
    assert lists.meant_list(said, personal) == meant


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
    assert await lists.parse_request("составь список дел на отпуск", ["Покупки", "Дела"], [], {}) is None


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
    assert "Покупки (общий): молоко 2 л, хлеб" in await desk.planner_text(people["mom"])


@pytest.mark.asyncio
async def test_a_tap_ticks_a_line_and_clearing_removes_it(db_sessions: async_sessionmaker[AsyncSession]) -> None:
    people = await _family(db_sessions)
    telegram = _telegram()
    desk = list_desk.ListDesk(telegram, db_sessions)
    await desk.view(people["mom"])
    overview, markup = _sent(telegram)
    assert overview == list_desk.OVERVIEW
    assert [b[0] for b in _buttons(markup)] == ["🛒 Покупки · 0", "🧹 Дела по дому · 0", "👤 Мои дела"]
    shopping = _buttons(markup)[0][1].split(":")[1]

    reply = MagicMock(spec=Message)
    reply.reply_to_message = MagicMock(text=list_desk.ADD_PROMPT.format(name="Покупки"))
    await desk.add_from_reply(people["mom"], reply, "молоко, хлеб\nяйца")
    assert "осталось 3" in _sent(telegram)[0]
    item = _buttons(_sent(telegram)[1])[0][1]

    tap = _callback(item)
    await desk.handle(tap, people["kid"], "flt", [item.split(":")[1]])
    shown = tap.message.edit_text.call_args
    assert "осталось 2, отмечено 1" in edited(tap.message) and "✅ молоко" in edited(tap.message)
    assert ("🧹 Убрать отмеченное", f"flc:{shopping}") in _buttons(shown.kwargs["reply_markup"])
    assert (await desk.brief_lines(people["mom"], date(2026, 11, 13))) == ["\n🛒 В покупках 2: хлеб, яйца"]
    assert await desk.brief_lines(people["kid"], date(2026, 11, 13)) == []

    clear = _callback(f"flc:{shopping}")
    await desk.handle(clear, people["mom"], "flc", [shopping])
    assert "осталось 2" in edited(clear.message) and "молоко" not in edited(clear.message)


@pytest.mark.asyncio
async def test_a_named_list_is_created_ticked_by_voice_and_gone_when_empty(
    db_sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    people = await _family(db_sessions)
    telegram = _telegram()
    desk = list_desk.ListDesk(telegram, db_sessions)
    _hears(monkeypatch, '{"action": "add", "list": "дача", "items": ["шланг"]}')
    await desk.request(people["mom"], "добавь шланг в список дача")
    assert "📝 Дача\nосталось 1" in _sent(telegram)[0] and await desk.names(people["mom"]) == (
        ["Покупки", "Дела по дому", "Дача"],
        [],
    )

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
    assert edited(clear.message) == list_desk.OVERVIEW and await desk.names(people["mom"]) == (
        ["Покупки", "Дела по дому"],
        [],
    )

    _hears(monkeypatch, '{"action": "show", "list": "Дача", "items": []}')
    await desk.request(people["mom"], "покажи список дача")
    text, markup = _sent(telegram)
    assert text == f"Списка «Дача» нет.\n\n{list_desk.OVERVIEW}"  # all the lists to pick from, not a dead end
    assert [b[0] for b in _buttons(markup)][:2] == ["🛒 Покупки · 0", "🧹 Дела по дому · 0"]


@pytest.mark.asyncio
@pytest.mark.parametrize("heard", ["Дела", "дела по дому", "Домашние дела"])
async def test_chores_said_any_usual_way_land_in_the_one_chores_list(
    db_sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch, heard: str
) -> None:
    people = await _family(db_sessions)
    telegram = _telegram()
    desk = list_desk.ListDesk(telegram, db_sessions)
    _hears(monkeypatch, f'{{"action": "add", "list": "{heard}", "items": ["помыть окна"]}}')
    assert await desk.request(people["kid"], "добавь помыть окна в дела") == (
        "✅ Добавила в «Дела по дому»: помыть окна"
    )
    assert _sent(telegram)[0].startswith("✅ Добавила в «Дела по дому»: помыть окна")
    assert await desk.add_from_chat(people["mom"], "Дела", ["вынести мусор"]) == (
        "Добавлено в «Дела по дому»: вынести мусор."
    )
    _hears(monkeypatch, '{"action": "done", "list": "Дела", "items": ["помыть окна"]}')
    await desk.request(people["mom"], "вычеркни окна из дел")
    assert "✅ Вычеркнула: помыть окна" in _sent(telegram)[0]
    assert await desk.names(people["mom"]) == (["Покупки", "Дела по дому"], [])  # no second «Дела» list


@pytest.mark.asyncio
async def test_my_list_named_by_the_model_stays_personal(db_sessions: async_sessionmaker[AsyncSession]) -> None:
    people = await _family(db_sessions)
    desk = list_desk.ListDesk(_telegram(), db_sessions)
    assert await desk.add_from_chat(people["mom"], "Мои дела", ["позвонить в банк"]) == (
        "Добавлено в «👤 Мои дела»: позвонить в банк."
    )
    assert await desk.names(people["mom"]) == (["Покупки", "Дела по дому"], ["Мои дела"])
    assert "банк" not in await desk.planner_text(people["kid"])


@pytest.mark.asyncio
async def test_a_ticked_line_nobody_cleared_goes_away_by_itself(db_sessions: async_sessionmaker[AsyncSession]) -> None:
    people = await _family(db_sessions)
    desk = list_desk.ListDesk(_telegram(), db_sessions)
    async with db_sessions() as db:
        shopping = await desk._named(db, people["mom"], "Покупки", personal=False, create=False)
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
    assert await desk.add_from_chat(people["mom"], "дела по дому", ["Повесить карниз ", "прикрутить полки"]) == (
        "Добавлено в «Дела по дому»: Повесить карниз, прикрутить полки."
    )
    assert await desk.add_from_chat(people["kid"], "Дела по дому", ["повесить карниз", "разобрать сумки"]) == (
        "Добавлено в «Дела по дому»: разобрать сумки. Уже были в списке: повесить карниз."
    )
    assert await desk.add_from_chat(people["mom"], "Дела по дому", ["  "]) == "Нечего добавлять."
    planned = await desk.planner_text(people["kid"])
    assert "Дела по дому (общий): Повесить карниз, прикрутить полки, разобрать сумки" in planned


@pytest.mark.asyncio
async def test_a_personal_list_is_seen_only_by_its_owner(
    db_sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    people = await _family(db_sessions)
    telegram = _telegram()
    desk = list_desk.ListDesk(telegram, db_sessions)
    _hears(monkeypatch, '{"action": "add", "list": "Мои дела", "items": ["записаться к врачу"], "personal": true}')
    assert await desk.request(people["mom"], "добавь в мой список записаться к врачу") == (
        "✅ Добавила в «👤 Мои дела»: записаться к врачу"
    )
    assert await desk.names(people["mom"]) == (["Покупки", "Дела по дому"], ["Мои дела"])
    assert await desk.names(people["kid"]) == (["Покупки", "Дела по дому"], [])
    assert "Мои дела (личный): записаться к врачу" in await desk.planner_text(people["mom"])
    assert "врачу" not in await desk.planner_text(people["kid"])
    assert await desk.brief_lines(people["mom"], date(2026, 11, 13)) == ["\n👤 Мои дела: записаться к врачу"]

    async with db_sessions() as db:
        mine = await db.scalar(select(AssistantFamilyList.id).where(AssistantFamilyList.owner_id == MOM))
        item = await db.scalar(select(AssistantFamilyListItem.id).where(AssistantFamilyListItem.list_id == mine))
    for prefix, args in (("flo", [mine]), ("fla", [mine]), ("flc", [mine]), ("flt", [item]), ("flt", [item, mine])):
        peek = _callback(":".join(map(str, [prefix, *args])))
        await desk.handle(peek, people["kid"], prefix, [str(arg) for arg in args])
        shown = edited(peek.message)  # the child gets only their own overview
        assert shown == f"{list_desk.LIST_GONE}\n\n{list_desk.OVERVIEW}"
        markup = peek.message.edit_text.call_args.kwargs["reply_markup"]
        assert f"flo:{mine}" not in [b[1] for b in _buttons(markup)]
    async with db_sessions() as db:
        still = await db.get(AssistantFamilyListItem, item)
        assert still is not None and still.done_at is None

    await desk.view(people["kid"])
    assert "Мои дела ·" not in str(_sent(telegram)[1])
    _hears(monkeypatch, '{"action": "add", "list": "Мои дела", "items": ["купить подарок маме"], "personal": true}')
    await desk.request(people["kid"], "добавь в мой список купить подарок маме")
    assert await desk.names(people["kid"]) == (["Покупки", "Дела по дому"], ["Мои дела"])
    assert "подарок" not in await desk.planner_text(people["mom"])


@pytest.mark.asyncio
async def test_the_my_list_button_opens_a_personal_list_and_the_chat_adds_to_it(
    db_sessions: async_sessionmaker[AsyncSession],
) -> None:
    people = await _family(db_sessions)
    desk = list_desk.ListDesk(_telegram(), db_sessions)
    tap = _callback("flm")
    await desk.handle(tap, people["mom"], "flm", [])
    assert edited(tap.message).startswith("👤 Мои дела\nосталось 0")
    assert await desk.add_from_chat(people["mom"], "", ["позвонить бухгалтеру"], personal=True) == (
        "Добавлено в «👤 Мои дела»: позвонить бухгалтеру."
    )
    reply = MagicMock(spec=Message)
    reply.reply_to_message = MagicMock(text=list_desk.ADD_PROMPT.format(name="👤 Мои дела"))
    await desk.add_from_reply(people["mom"], reply, "сдать отчёт")
    family, mine = await desk.names(people["mom"])
    assert mine == ["Мои дела"] and "сдать отчёт" in await desk.planner_text(people["mom"])


@pytest.mark.asyncio
async def test_the_list_screen_goes_away_when_its_add_prompt_opens(
    db_sessions: async_sessionmaker[AsyncSession],
) -> None:
    people = await _family(db_sessions)
    telegram = _telegram()
    desk = list_desk.ListDesk(telegram, db_sessions)
    await desk.view(people["mom"])
    shopping = _buttons(_sent(telegram)[1])[0][1].split(":")[1]
    tap = _callback(f"fla:{shopping}")
    tap.message.delete = AsyncMock()
    await desk.handle(tap, people["mom"], "fla", [shopping])
    assert _sent(telegram)[0] == list_desk.ADD_PROMPT.format(name="Покупки")
    assert prompt_cancel(_sent(telegram)[1]) == f"{list_desk.LIST_CANCEL}:{shopping}"
    tap.message.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelling_the_add_prompt_brings_the_list_back(db_sessions: async_sessionmaker[AsyncSession]) -> None:
    people = await _family(db_sessions)
    telegram = _telegram()
    desk = list_desk.ListDesk(telegram, db_sessions)
    await desk.view(people["kid"])
    shopping = _buttons(_sent(telegram)[1])[0][1].split(":")[1]
    await desk.handle(_callback(f"fla:{shopping}"), people["kid"], "fla", [shopping])
    cancel = _callback(prompt_cancel(_sent(telegram)[1]))
    await desk.handle(cancel, people["kid"], list_desk.LIST_CANCEL, [shopping])
    cancel.message.delete.assert_awaited_once()
    text, markup = _sent(telegram)
    assert text.startswith("🛒 Покупки") and ("➕ Добавить", f"fla:{shopping}") in _buttons(markup)


@pytest.mark.asyncio
async def test_cancelling_for_a_list_that_went_away_shows_all_lists(
    db_sessions: async_sessionmaker[AsyncSession],
) -> None:
    people = await _family(db_sessions)
    telegram = _telegram()
    desk = list_desk.ListDesk(telegram, db_sessions)
    cancel = _callback(f"{list_desk.LIST_CANCEL}:9999")
    await desk.handle(cancel, people["kid"], list_desk.LIST_CANCEL, ["9999"])
    text, markup = _sent(telegram)
    assert text.startswith(list_desk.LIST_GONE) and list_desk.OVERVIEW in text
    assert [b[0] for b in _buttons(markup)][:2] == ["🛒 Покупки · 0", "🧹 Дела по дому · 0"]


@pytest.mark.asyncio
async def test_an_empty_answer_brings_a_fresh_prompt_that_still_works(
    db_sessions: async_sessionmaker[AsyncSession],
) -> None:
    people = await _family(db_sessions)
    telegram = _telegram()
    desk = list_desk.ListDesk(telegram, db_sessions)
    await desk.view(people["mom"])
    shopping = _buttons(_sent(telegram)[1])[0][1].split(":")[1]
    reply = MagicMock(spec=Message)
    prompt = list_desk.ADD_PROMPT.format(name="Покупки")
    reply.reply_to_message = MagicMock(text=prompt, from_user=MagicMock(is_bot=True))
    await desk.add_from_reply(people["mom"], reply, " , ; ")
    fresh, markup = _sent(telegram)
    assert fresh == f"{list_desk.NOTHING_TO_ADD}\n{prompt}"
    assert prompt_cancel(markup) == f"{list_desk.LIST_CANCEL}:{shopping}"

    again = MagicMock(spec=Message)
    again.reply_to_message = MagicMock(text=fresh, from_user=MagicMock(is_bot=True))
    assert desk.is_add_reply(again)
    await desk.add_from_reply(people["mom"], again, "молоко")
    assert "✅ Добавила в «Покупки»: молоко" in _sent(telegram)[0]


async def _shopping(desk: list_desk.ListDesk, member: AssistantMember) -> AssistantFamilyList:
    async with desk._sessions() as db:
        found = await desk._named(db, member, "Покупки", personal=False, create=False)
    assert found is not None
    return found


@pytest.mark.asyncio
async def test_adding_what_is_already_there_says_so_not_that_the_list_is_full(
    db_sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    people = await _family(db_sessions)
    telegram = _telegram()
    desk = list_desk.ListDesk(telegram, db_sessions)
    reply = MagicMock(spec=Message)
    reply.reply_to_message = MagicMock(text=list_desk.ADD_PROMPT.format(name="Покупки"))
    await desk.add_from_reply(people["mom"], reply, "молоко, хлеб")
    await desk.add_from_reply(people["kid"], reply, "молоко, хлеб")
    note = _sent(telegram)[0]
    assert note.startswith("Уже есть в «Покупки»: молоко, хлеб") and list_desk.LIST_FULL not in note
    await desk.add_from_reply(people["kid"], reply, "Хлеб, сыр")
    assert _sent(telegram)[0].startswith("✅ Добавила в «Покупки»: сыр. Уже есть: Хлеб")
    _hears(monkeypatch, '{"action": "add", "list": "Покупки", "items": ["молоко", "хлеб"]}')
    assert await desk.request(people["kid"], "добавь молоко и хлеб в покупки") == "Уже есть в «Покупки»: молоко, хлеб"


@pytest.mark.asyncio
async def test_a_full_list_says_what_did_not_fit(db_sessions: async_sessionmaker[AsyncSession]) -> None:
    people = await _family(db_sessions)
    telegram = _telegram()
    desk = list_desk.ListDesk(telegram, db_sessions)
    shopping = await _shopping(desk, people["mom"])
    async with db_sessions() as db:
        added = await desk._add(db, shopping, [f"пункт {n}" for n in range(list_desk.MAX_OPEN_ITEMS - 1)], MOM)
    assert len(added.added) == list_desk.MAX_OPEN_ITEMS - 1
    reply = MagicMock(spec=Message)
    reply.reply_to_message = MagicMock(text=list_desk.ADD_PROMPT.format(name="Покупки"))
    await desk.add_from_reply(people["kid"], reply, "сыр, пункт 1, чай")
    note = _sent(telegram)[0]
    assert note.startswith("✅ Добавила в «Покупки»: сыр. Уже есть: пункт 1. Не поместилось: чай. ")
    assert list_desk.LIST_FULL in note
    await desk.add_from_reply(people["kid"], reply, "кофе")
    assert _sent(telegram)[0].startswith(f"Не поместилось: кофе. {list_desk.LIST_FULL}")
    assert await desk.add_from_chat(people["mom"], "Покупки", ["кофе", "сыр"]) == (
        f"Ничего нового в «Покупки». Уже были в списке: сыр. "
        f"Не добавлено, список полон ({list_desk.MAX_OPEN_ITEMS} пунктов): кофе."
    )


@pytest.mark.asyncio
async def test_a_line_cleared_on_another_screen_redraws_its_list(
    db_sessions: async_sessionmaker[AsyncSession],
) -> None:
    people = await _family(db_sessions)
    telegram = _telegram()
    desk = list_desk.ListDesk(telegram, db_sessions)
    shopping = await _shopping(desk, people["mom"])
    async with db_sessions() as db:
        await desk._add(db, shopping, ["молоко", "хлеб"], MOM)
    await desk._send_list(people["kid"], shopping.id)  # Dad's screen, still open
    milk = _buttons(_sent(telegram)[1])[0][1]
    assert milk.endswith(f":{shopping.id}")  # a line's button knows its list

    mom_tick = _callback(milk)
    await desk.handle(mom_tick, people["mom"], "flt", milk.split(":")[1:])
    await desk.handle(_callback(f"flc:{shopping.id}"), people["mom"], "flc", [str(shopping.id)])

    late = _callback(milk)
    await desk.handle(late, people["kid"], "flt", milk.split(":")[1:])
    shown = edited(late.message)
    assert shown.startswith(f"{list_desk.ITEM_GONE}\n🛒 Покупки") and "осталось 1" in shown
    markup = late.message.edit_text.call_args.kwargs["reply_markup"]
    assert ("⬅️ Все списки", "flb") in _buttons(markup)


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["fla", "flc", "flo", "flt"])
async def test_a_button_on_a_list_that_went_away_leads_to_all_lists(
    db_sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch, prefix: str
) -> None:
    people = await _family(db_sessions)
    telegram = _telegram()
    desk = list_desk.ListDesk(telegram, db_sessions)
    _hears(monkeypatch, '{"action": "add", "list": "Дача", "items": ["шланг"]}')
    await desk.request(people["mom"], "добавь шланг в список дача")
    async with db_sessions() as db:
        dacha = await db.scalar(select(AssistantFamilyList.id).where(AssistantFamilyList.name == "Дача"))
        item = await db.scalar(select(AssistantFamilyListItem.id).where(AssistantFamilyListItem.list_id == dacha))
    await desk.handle(_callback(f"flt:{item}:{dacha}"), people["mom"], "flt", [str(item), str(dacha)])
    await desk.handle(_callback(f"flc:{dacha}"), people["mom"], "flc", [str(dacha)])
    assert await desk.names(people["mom"]) == (["Покупки", "Дела по дому"], [])

    data = f"flt:{item}:{dacha}" if prefix == "flt" else f"{prefix}:{dacha}"  # on the child's older screen
    late = _callback(data)
    late.message.delete = AsyncMock()
    await desk.handle(late, people["kid"], prefix, data.split(":")[1:])
    assert edited(late.message) == f"{list_desk.LIST_GONE}\n\n{list_desk.OVERVIEW}"
    markup = late.message.edit_text.call_args.kwargs["reply_markup"]
    assert [b[0] for b in _buttons(markup)][:2] == ["🛒 Покупки · 0", "🧹 Дела по дому · 0"]
    late.message.delete.assert_not_awaited()
