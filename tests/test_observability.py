"""Langfuse in Luna: feedback reactions, the API client, the evaluation suites and the weekly model health."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from aiogram.types import ReactionTypeCustomEmoji, ReactionTypeEmoji
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lunagpt.assistant import feedback, model_health, usage
from lunagpt.assistant.feedback import FeedbackDesk, verdict
from lunagpt.evals import runner, suites
from lunagpt.evals.runner import SuiteResult
from lunagpt.langfuse_api import Langfuse
from lunagpt.models import AssistantMember, AssistantReplyTrace

OWNER = 7


class FakeLangfuse:
    """Records what Luna sends instead of sending it."""

    enabled = True

    def __init__(self) -> None:
        self.scores: dict[str, tuple[str, str, float, str | None]] = {}
        self.deleted: list[str] = []
        self.items: list[str] = []
        self.links: list[tuple[str, str]] = []

    async def score(self, score_id: str, trace_id: str, name: str, value: float, comment: str | None = None) -> bool:
        self.scores[score_id] = (trace_id, name, value, comment)
        return True

    async def delete_score(self, score_id: str) -> bool:
        self.deleted.append(score_id)
        self.scores.pop(score_id, None)
        return True

    async def ensure_dataset(self, name: str, description: str) -> bool:
        return True

    async def upsert_item(self, dataset: str, item_id: str, input: Any, expected: Any) -> bool:
        self.items.append(item_id)
        return True

    async def trace(self, trace_id: str, name: str, input: Any, output: Any, tags: list[str]) -> bool:
        return True

    async def link_run_item(self, run: str, item_id: str, trace_id: str, metadata: dict[str, Any]) -> bool:
        self.links.append((run, item_id))
        return True


def _reaction(message_id: int, *emojis: str, chat_id: int = OWNER) -> MagicMock:
    event = MagicMock()
    event.chat.id = chat_id
    event.message_id = message_id
    event.user.is_bot = False
    event.new_reaction = [ReactionTypeEmoji(emoji=e) for e in emojis]
    return event


@pytest.fixture
async def sessions(db_sessions: async_sessionmaker[AsyncSession]) -> async_sessionmaker[AsyncSession]:
    async with db_sessions() as db:
        db.add(AssistantMember(telegram_id=OWNER, name="Хозяин", role="owner"))
        await db.commit()
    return db_sessions


def test_reactions_read_as_good_bad_or_no_opinion() -> None:
    assert verdict([ReactionTypeEmoji(emoji="👍")]) == (1, "👍")
    assert verdict([ReactionTypeEmoji(emoji="❤")]) == (1, "❤")
    assert verdict([ReactionTypeEmoji(emoji="👎")]) == (0, "👎")
    assert verdict([ReactionTypeEmoji(emoji="🔥"), ReactionTypeEmoji(emoji="💩")]) == (0, "💩")  # a doubt wins
    assert verdict([ReactionTypeEmoji(emoji="🎃")]) == (None, "")
    assert verdict([ReactionTypeCustomEmoji(custom_emoji_id="1")]) == (None, "")
    assert verdict([]) == (None, "")


@pytest.mark.asyncio
async def test_a_reaction_scores_the_reply_and_can_change_or_go(sessions: Any) -> None:
    langfuse = FakeLangfuse()
    desk = FeedbackDesk(sessions, langfuse)
    await desk.remember(OWNER, [10, 11], "trace-1")

    await desk.on_reaction(_reaction(11, "👎"))
    assert langfuse.scores == {"trace-1-feedback": ("trace-1", "user-feedback", 0, "👎")}
    await desk.on_reaction(_reaction(11, "👍"))  # changed its mind: one score, replaced
    assert langfuse.scores == {"trace-1-feedback": ("trace-1", "user-feedback", 1, "👍")}
    await desk.on_reaction(_reaction(11))  # took the reaction away
    assert langfuse.scores == {} and langfuse.deleted == ["trace-1-feedback"]


@pytest.mark.asyncio
async def test_reactions_elsewhere_are_ignored(sessions: Any) -> None:
    langfuse = FakeLangfuse()
    desk = FeedbackDesk(sessions, langfuse)
    await desk.remember(OWNER, [10], "trace-1")
    await desk.on_reaction(_reaction(99, "👍"))  # a menu or a reminder, not a reply
    bot_event = _reaction(10, "👍")
    bot_event.user.is_bot = True
    await desk.on_reaction(bot_event)
    assert langfuse.scores == {}


@pytest.mark.asyncio
async def test_old_reply_traces_are_forgotten(sessions: Any) -> None:
    desk = FeedbackDesk(sessions, FakeLangfuse())
    await desk.remember(OWNER, [1], "old")
    async with sessions() as db:
        await db.execute(update(AssistantReplyTrace).values(created_at=datetime.now(UTC) - timedelta(days=31)))
        await db.commit()
    await desk.remember(OWNER, [2], "new")
    async with sessions() as db:
        assert list(await db.scalars(select(AssistantReplyTrace.trace_id))) == ["new"]


@pytest.mark.asyncio
async def test_without_langfuse_nothing_is_kept(sessions: Any) -> None:
    desk = FeedbackDesk(sessions, Langfuse(host="", public_key="", secret_key=""))
    await desk.remember(OWNER, [1], "trace")
    await desk.on_reaction(_reaction(1, "👍"))
    async with sessions() as db:
        assert list(await db.scalars(select(AssistantReplyTrace))) == []


def _api(handler: Any) -> Langfuse:
    return Langfuse("http://langfuse", "pk", "sk", transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_api_scores_by_id_and_fails_soft() -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(207, json={"successes": [], "errors": []})

    assert await _api(handler).score("s1", "t1", "user-feedback", 1, "👍")
    body = sent[0]["batch"][0]
    assert body["type"] == "score-create" and body["body"]["id"] == "s1" and body["body"]["traceId"] == "t1"
    assert not await _api(lambda request: httpx.Response(500)).score("s1", "t1", "x", 1)


@pytest.mark.asyncio
async def test_api_reads_every_page_of_generations() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/health"):
            return httpx.Response(200, json={"status": "OK"})
        page = int(request.url.params["page"])
        return httpx.Response(200, json={"data": [{"id": f"g{page}"}], "meta": {"totalPages": 2}})

    rows = await _api(handler).generations(datetime(2026, 11, 12, tzinfo=UTC))
    assert rows == [{"id": "g1"}, {"id": "g2"}]
    assert await _api(lambda request: httpx.Response(503)).generations(datetime.now(UTC)) is None


def test_suites_have_unique_cases_and_checks_catch_mistakes() -> None:
    for suite in suites.SUITES:
        ids = [case.id for case in suite.cases]
        assert len(ids) == len(set(ids)), suite.name
    memory = {s.name: s for s in suites.SUITES}["memory"].check
    assert memory(["Пользователь едет в горы 19 ноября 2026"], {"required": [["19 ноября"]], "forbidden": ["завтра"]})[
        0
    ]
    assert not memory(["Пользователь едет в горы завтра"], {"required": [["19 ноября"]], "forbidden": ["завтра"]})[0]
    assert not memory(["Пользователь живёт в Ташкенте"], {"required": [], "forbidden": []})[0]
    reminder = {s.name: s for s in suites.SUITES}["reminders"].check
    expected = {"due": ["2026-11-19 08:00"], "repeat": "none", "task": "хлеб"}
    assert reminder({"task": "купить хлеб", "repeat": "none", "due": "2026-11-19 08:00"}, expected)[0]
    assert not reminder({"task": "купить хлеб", "repeat": "none", "due": "2026-11-19 20:00"}, expected)[0]
    lists_check = {s.name: s for s in suites.SUITES}["lists"].check
    assert lists_check(None, {"action": None})[0]
    assert not lists_check({"action": "add"}, {"action": None})[0]


@pytest.mark.asyncio
async def test_a_run_scores_and_links_every_case() -> None:
    async def task(given: dict[str, Any], metadata: dict[str, Any]) -> str:
        assert "client:luna-eval" in metadata["tags"]  # evaluation text is made up, so it is never redacted
        if given["text"] == "boom":
            raise ValueError("model down")
        return str(given["text"])

    suite = suites.Suite(
        "demo",
        "",
        (
            suites.Case("a", {"text": "ok"}, {}),
            suites.Case("b", {"text": "no"}, {}),
            suites.Case("c", {"text": "boom"}, {}),
        ),
        task,
        lambda output, expected: (output == "ok", "" if output == "ok" else "wrong"),
    )
    langfuse = FakeLangfuse()
    result = await runner.run_suite(suite, langfuse, "run-1")
    assert (result.passed, result.total) == (1, 3)
    assert sorted(result.failures) == ["b: wrong", "c: error: ValueError: model down"]
    assert sorted(value for _, _, value, _ in langfuse.scores.values()) == [0, 0, 1]
    assert sorted(langfuse.links) == [("run-1", "demo-a"), ("run-1", "demo-b"), ("run-1", "demo-c")]


def _gen(trace: str, model: str, seconds: float, client: str = "luna-assistant", error: bool = False) -> dict[str, Any]:
    start = datetime(2026, 11, 19, 10, tzinfo=UTC)
    return {
        "traceId": trace,
        "model": model,
        "level": "ERROR" if error else "DEFAULT",
        "startTime": start.isoformat(),
        "endTime": (start + timedelta(seconds=seconds)).isoformat(),
        "metadata": {"tags": [f"client:{client}"]},
    }


def test_model_lines_show_failures_rescues_and_slow_models() -> None:
    generations = [_gen(f"t{i}", "claude-5.5", 2.0 + i) for i in range(6)]  # a dot in a name stays a dot
    generations += [_gen("t0", "gemma", 30, error=True)]  # failed first, Claude answered the same trace
    generations += [_gen("h1", "claude", 1, client="homesec")]
    generations += [_gen("e1", "claude", 1, client="luna-eval", error=True)]  # checks are not the family's use
    lines = model_health.model_lines(generations)
    assert lines[0] == "• Luna: 7 вызовов, ошибок 1 — gemma 1; запасная модель выручила 1 раз"
    assert lines[1] == "• HomeSec: 1 вызовов, ошибок нет"
    assert lines[2] == "• 95% ответов быстрее: claude-5.5 7,0 с"


def test_homesec_calls_without_tags_are_known_by_their_sdk() -> None:
    assert model_health.client_of({"metadata": {"user_agent": "Anthropic/Python 0.40"}}) == "homesec"
    assert model_health.client_of({"metadata": {}}) == "other"


def test_feedback_and_eval_lines() -> None:
    scores = [
        {"value": 1, "trace": {"tags": ["feature:chat"]}},
        {"value": 0, "trace": {"tags": ["feature:photo"]}},
        {"value": 0, "trace": {"tags": ["feature:photo"]}},
    ]
    assert model_health.feedback_line(scores) == "👍 1 · 👎 2 — 👎 на фото 2"
    assert "никто не ставил" in model_health.feedback_line([])
    lines = model_health.eval_lines([SuiteResult("memory", 19, 20, ["tomorrow: missing"]), SuiteResult("lists", 9, 9)])
    assert lines == ["🧪 Проверки Luna: память 19/20, списки 9/9", "   ✗ память: tomorrow: missing"]


@pytest.mark.asyncio
async def test_weekly_report_carries_the_extra_block_and_survives_its_failure(
    sessions: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot = MagicMock(send_message=AsyncMock())
    sunday = datetime(2026, 11, 22, 20, 30)
    assert await usage.send_weekly_report(bot, sessions, sunday, AsyncMock(return_value=["", "🩺 Модели за неделю"]))
    assert bot.send_message.await_args.args[1].endswith("🩺 Модели за неделю")

    async with sessions() as db:
        await db.execute(update(AssistantMember).values(report_sent_on=None))
        await db.commit()
    assert await usage.send_weekly_report(bot, sessions, sunday, AsyncMock(side_effect=RuntimeError))
    assert "📊 Luna за неделю" in bot.send_message.await_args.args[1]


def test_feedback_score_name_matches_the_report() -> None:
    assert feedback.SCORE_NAME == "user-feedback"
