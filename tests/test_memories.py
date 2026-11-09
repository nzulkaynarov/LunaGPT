"""Long-term memory against a real Postgres database created for this run."""

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lunagpt.assistant import llm, memories
from lunagpt.assistant.memories import Memory
from lunagpt.models import AssistantMember, AssistantMemory

OWNER, KID = 101, 102


def vector_for(fact: str) -> list[float]:
    """Facts that share a topic word point the same way; 'Город2' is close but not identical to 'Город'."""
    base = [0.0] * 1024
    if "город" in fact.lower():
        base[0] = 1.0
        base[1] = 0.6 if "Самарканд" in fact else 0.0
    elif "игр" in fact.lower():
        base[2] = 1.0
    else:
        base[3 + abs(hash(fact)) % 1000] = 1.0
    return base


@pytest_asyncio.fixture
async def sessions(db_sessions: async_sessionmaker[AsyncSession]) -> async_sessionmaker[AsyncSession]:
    async with db_sessions() as db:
        db.add_all(
            [
                AssistantMember(telegram_id=OWNER, name="Владелец", role="owner"),
                AssistantMember(telegram_id=KID, name="Ребёнок", role="kid"),
            ]
        )
        await db.commit()
    return db_sessions


def _memory(sessions: async_sessionmaker[AsyncSession], *replies: Any) -> tuple[Memory, AsyncMock]:
    complete = AsyncMock(side_effect=list(replies))
    return Memory(sessions, complete=complete, embed=AsyncMock(side_effect=vector_for)), complete


def _facts(*facts: str) -> str:
    return json.dumps({"facts": list(facts)}, ensure_ascii=False)


async def _contents(memory: Memory, member: int) -> list[str]:
    return [row.content for row in await memory.list_facts(member)]


@pytest.mark.asyncio
async def test_new_fact_is_stored_and_recalled(sessions: Any) -> None:
    memory, _ = _memory(sessions, _facts("Пользователь любит игры"))
    await memory.remember(OWNER, "owner", "Я очень люблю компьютерные игры")
    assert await memory.recall(OWNER, "owner", "во что поиграть?") == ["Пользователь любит игры"]


@pytest.mark.asyncio
async def test_facts_never_cross_between_people(sessions: Any) -> None:
    memory, _ = _memory(sessions, _facts("Пользователь любит игры"))
    await memory.remember(OWNER, "owner", "Я очень люблю компьютерные игры")
    assert await memory.recall(KID, "kid", "во что поиграть?") == []
    facts = await memory.list_facts(OWNER)
    assert await memory.forget(KID, facts[0].id) is False
    assert len(await memory.list_facts(OWNER)) == 1


@pytest.mark.asyncio
async def test_same_fact_is_not_duplicated(sessions: Any) -> None:
    memory, complete = _memory(sessions, _facts("Пользователь любит игры"), _facts("Пользователь любит игры"))
    await memory.remember(OWNER, "owner", "Я очень люблю компьютерные игры")
    await memory.remember(OWNER, "owner", "Обожаю игры, правда")
    assert len(await _contents(memory, OWNER)) == 1
    assert complete.await_count == 2  # duplicates need no consolidation call


@pytest.mark.asyncio
async def test_changed_value_replaces_the_old_fact(sessions: Any) -> None:
    memory, _ = _memory(
        sessions,
        _facts("Пользователь: город Ташкент"),
        _facts("Пользователь: город Самарканд"),
        json.dumps({"action": "replace", "index": 0}),
    )
    await memory.remember(OWNER, "owner", "Я живу в городе Ташкент")
    await memory.remember(OWNER, "owner", "Я переехал в город Самарканд")
    assert await _contents(memory, OWNER) == ["Пользователь: город Самарканд"]


@pytest.mark.parametrize("decision", ['{"action": "add"}', "мусор", '{"action": "replace", "index": 9}', None])
@pytest.mark.asyncio
async def test_unclear_consolidation_keeps_both_facts(sessions: Any, decision: str | None) -> None:
    memory, _ = _memory(
        sessions, _facts("Пользователь: город Ташкент"), _facts("Пользователь: город Самарканд"), decision
    )
    await memory.remember(OWNER, "owner", "Я живу в городе Ташкент")
    await memory.remember(OWNER, "owner", "Мне нравится город Самарканд")
    assert len(await _contents(memory, OWNER)) == 2


@pytest.mark.asyncio
async def test_guest_has_personal_memory(sessions: Any) -> None:
    memory, _ = _memory(sessions, _facts("Пользователь любит игры"))
    await memory.remember(OWNER, "guest", "Я гость и люблю игры")
    assert await memory.recall(OWNER, "guest", "кто я") == ["Пользователь любит игры"]


@pytest.mark.asyncio
async def test_short_messages_skip_the_model(sessions: Any) -> None:
    memory, complete = _memory(sessions)
    await memory.remember(OWNER, "owner", "спасибо")
    complete.assert_not_awaited()


@pytest.mark.parametrize("raw", ["не json", None, '{"facts": "строка"}'])
@pytest.mark.asyncio
async def test_bad_extraction_stores_nothing(sessions: Any, raw: str | None) -> None:
    memory, _ = _memory(sessions, raw)
    await memory.remember(OWNER, "owner", "Я очень люблю компьютерные игры")
    assert await _contents(memory, OWNER) == []


@pytest.mark.asyncio
async def test_memory_failure_does_not_raise(sessions: Any) -> None:
    memory = Memory(sessions, complete=AsyncMock(side_effect=RuntimeError), embed=AsyncMock())
    await memory.remember(OWNER, "owner", "Я очень люблю компьютерные игры")


@pytest.mark.asyncio
async def test_large_store_uses_similarity(sessions: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(memories, "MAX_FACTS_IN_PROMPT", 2)
    memory, _ = _memory(sessions)
    async with sessions() as db:
        for fact in ("Пользователь: город Ташкент", "Любит игры", "Сад", "Море"):
            db.add(AssistantMemory(member_id=OWNER, content=fact, embedding=vector_for(fact)))
        await db.commit()
    found = await memory.recall(OWNER, "owner", "Какие игры?")
    assert len(found) == 2 and "Любит игры" in found


@pytest.mark.asyncio
async def test_store_is_capped(sessions: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(memories, "MAX_FACTS_PER_MEMBER", 2)
    memory, _ = _memory(sessions, _facts("Сад"), _facts("Море"), _facts("Горы"))
    for text_value in ("Расскажу про сад", "Расскажу про море", "Расскажу про горы"):
        await memory.remember(OWNER, "owner", text_value)
    assert len(await _contents(memory, OWNER)) == 2


@pytest.mark.asyncio
async def test_history_survives_restart_and_is_trimmed(sessions: Any) -> None:
    memory, _ = _memory(sessions)
    for index in range(3):
        await memory.save_exchange(OWNER, f"вопрос {index}", f"ответ {index}", 4)
    restored = await Memory(sessions).load_history(OWNER, 8)
    assert [m["content"] for m in restored] == ["вопрос 1", "ответ 1", "вопрос 2", "ответ 2"]
    assert restored[0]["role"] == "user"
    assert await Memory(sessions).load_history(KID, 8) == []


@pytest.mark.asyncio
async def test_forget_all_clears_only_this_person(sessions: Any) -> None:
    memory, _ = _memory(sessions, _facts("Сад"), _facts("Море"))
    await memory.remember(OWNER, "owner", "Расскажу про сад")
    await memory.remember(KID, "kid", "Расскажу про море")
    await memory.save_exchange(OWNER, "вопрос", "ответ", 8)
    await memory.forget_all(OWNER)
    assert await _contents(memory, OWNER) == []
    assert await memory.load_history(OWNER, 8) == []
    assert await _contents(memory, KID) == ["Море"]


@pytest.mark.asyncio
async def test_removing_a_member_erases_their_memory(sessions: Any) -> None:
    memory, _ = _memory(sessions, _facts("Сад"))
    await memory.remember(KID, "kid", "Расскажу про сад")
    async with sessions() as db:
        await db.delete(await db.get(AssistantMember, KID))
        await db.commit()
    assert await _contents(memory, KID) == []


@pytest.mark.asyncio
async def test_facts_reach_the_system_prompt_as_notes(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_run(_client: Any, _model: str, messages: list[dict[str, Any]], *_args: Any) -> str:
        captured["system"] = messages[0]["content"]
        return "ответ"

    monkeypatch.setattr(llm, "_run_model", fake_run)
    await llm.generate_reply("owner", 1, [], "привет", ["Пользователь любит игры"])
    assert "Пользователь любит игры" in captured["system"] and "не инструкции" in captured["system"]


@pytest.mark.asyncio
async def test_memory_traces_keep_family_text_redacted() -> None:
    assert "client:luna-assistant" in memories._metadata(1, "kid")["tags"]
