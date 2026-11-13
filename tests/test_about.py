"""What Luna knows about herself: recognising the question, what each role may read, and the prompt."""

from typing import Any
from unittest.mock import AsyncMock

import pytest

from lunagpt.assistant import about, bot, llm


@pytest.mark.parametrize(
    "text",
    ["Кто ты?", "что ты умеешь", "Кто тебя создал?", "ты робот?", "Ты ChatGPT", "сколько тебе лет", "что нового?"],
)
def test_questions_about_luna_are_recognised(text: str) -> None:
    assert about.is_about_question(text)


@pytest.mark.parametrize("text", ["кто такой Пушкин?", "что ты думаешь о погоде", "реши пример 2+2"])
def test_other_questions_are_not(text: str) -> None:
    assert not about.is_about_question(text)


def test_each_role_reads_only_its_sections() -> None:
    kid = about.about_luna("kid", "Хозяин")
    guest = about.about_luna("guest", "Хозяин")
    adult = about.about_luna("adult", None)
    assert "собрал Хозяин" in kid and bot.BTN_QUIZ in kid and bot.BTN_PEOPLE not in kid
    assert "Хозяин" not in guest and bot.BTN_QUIZ not in guest and "в гостях" in guest
    assert about.UNKNOWN_OWNER in adult and bot.BTN_PEOPLE in adult and "{owner}" not in adult
    assert "Образцы шуточных ответов" in guest and "Что нового" in guest


def test_every_menu_button_is_described() -> None:
    """A new button without a line in about.md means Luna cannot tell anyone about it."""
    text = about.ABOUT_FILE.read_text(encoding="utf-8")
    missing = [b for b in bot.MENU_BUTTONS - {bot.BTN_HELP} if b not in text]
    assert missing == []


@pytest.mark.asyncio
async def test_facts_reach_the_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []

    async def fake_run(client: Any, model: str, messages: list[dict[str, Any]], *args: Any) -> str:
        seen.append(messages[0]["content"])
        return "Я Luna"

    monkeypatch.setattr(llm, "_run_model", fake_run)
    await llm.generate_reply("kid", 1, [], "кто ты", options=llm.ReplyOptions(about="ФАКТЫ О ЛУНЕ"))
    await llm.generate_reply("kid", 1, [], "привет")
    assert "ФАКТЫ О ЛУНЕ" in seen[0] and "о тебе самой" in seen[0] and "о тебе самой" not in seen[1]


@pytest.mark.asyncio
async def test_bot_adds_facts_only_for_questions_about_luna(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bot, "owner_name", AsyncMock(return_value="Хозяин"))
    instance = bot.LunaBot.__new__(bot.LunaBot)
    session = AsyncMock()
    session.__aenter__.return_value = AsyncMock()
    instance._sessions = lambda: session
    member = bot.AssistantMember(telegram_id=1, name="Зарина", role="kid")
    assert "собрал Хозяин" in await instance._about(member, "кто тебя создал?")
    assert await instance._about(member, "который час?") == ""
