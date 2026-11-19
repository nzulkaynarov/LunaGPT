"""The owner's weekly look at the models, read back from Langfuse: what failed, what was slow, what the family
liked, and whether Luna still passes her checks. Every source is optional; a missing one says so in one line."""

import logging
import math
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from lunagpt.evals.runner import SuiteResult, run_all
from lunagpt.langfuse_api import Langfuse

logger = logging.getLogger("lunagpt.assistant.model_health")

WEEK = timedelta(days=7)
CLIENT_NAMES = {"luna-assistant": "Luna", "homesec": "HomeSec", "other": "без пометки (тесты, ручные вызовы)"}
SUITE_NAMES = {"memory": "память", "reminders": "напоминания", "calendar": "календарь", "lists": "списки"}
FEATURE_NAMES = {"chat": "в чате", "photo": "на фото"}
MAX_FAILURES_SHOWN = 3


def client_of(generation: dict[str, Any]) -> str:
    """Luna and evaluations tag their calls; HomeSec's older calls are known by its Anthropic SDK."""
    metadata = generation.get("metadata") or {}
    for tag in metadata.get("tags") or []:
        if isinstance(tag, str) and tag.startswith("client:"):
            return tag.removeprefix("client:")
    agent = str(metadata.get("user_agent") or "")
    return "homesec" if agent.startswith("Anthropic/") else "other"


def _seconds(generation: dict[str, Any]) -> float | None:
    try:
        start = datetime.fromisoformat(str(generation["startTime"]).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(generation["endTime"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return None
    return (end - start).total_seconds()


def p95(values: Sequence[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def _model(generation: dict[str, Any]) -> str:
    return str(generation.get("model") or "?")


def model_lines(generations: list[dict[str, Any]]) -> list[str]:
    """Calls, failures and rescues per client, and the slowest models; evaluation calls are left out."""
    real = [g for g in generations if client_of(g) != "luna-eval"]
    if not real:
        return ["• Вызовов моделей не было."]
    lines = []
    by_client: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for generation in real:
        by_client[client_of(generation)].append(generation)
    for client, calls in sorted(by_client.items(), key=lambda item: -len(item[1])):
        failed = Counter(_model(g) for g in calls if g.get("level") == "ERROR")
        name = CLIENT_NAMES.get(client, client)
        if failed:
            worst = ", ".join(f"{model} {count}" for model, count in failed.most_common(3))
            line = f"• {name}: {len(calls)} вызовов, ошибок {sum(failed.values())} — {worst}"
        else:
            line = f"• {name}: {len(calls)} вызовов, ошибок нет"
        traces: dict[str, set[str]] = defaultdict(set)
        for generation in calls:
            traces[str(generation.get("traceId"))].add("error" if generation.get("level") == "ERROR" else "ok")
        rescued = sum(1 for states in traces.values() if states == {"error", "ok"})
        if rescued:
            line += f"; запасная модель выручила {rescued} раз"
        lines.append(line)
    durations: dict[str, list[float]] = defaultdict(list)
    for generation in real:
        seconds = _seconds(generation)
        if seconds is not None and generation.get("level") != "ERROR":
            durations[_model(generation)].append(seconds)
    slow = sorted(((p95(v), model) for model, v in durations.items() if len(v) >= 5), reverse=True)[:3]
    if slow:
        lines.append(
            "• 95% ответов быстрее: "
            + ", ".join(f"{model} {f'{seconds:.1f}'.replace('.', ',')} с" for seconds, model in slow)
        )
    return lines


def feedback_line(scores: list[dict[str, Any]]) -> str:
    if not scores:
        return "👍/👎 на ответы Luna за неделю никто не ставил."
    good = sum(1 for s in scores if s.get("value") == 1)
    bad = [s for s in scores if s.get("value") == 0]
    line = f"👍 {good} · 👎 {len(bad)}"
    where: Counter[str] = Counter()
    for score in bad:
        tags = (score.get("trace") or {}).get("tags") or []
        feature = next((t.removeprefix("feature:") for t in tags if t.startswith("feature:")), "")
        where[FEATURE_NAMES.get(feature, feature or "без пометки")] += 1
    if where:
        line += " — " + ", ".join(f"👎 {name} {count}" for name, count in where.most_common())
    return line


def eval_lines(results: Sequence[SuiteResult]) -> list[str]:
    if not results:
        return []
    summary = ", ".join(f"{SUITE_NAMES.get(r.name, r.name)} {r.passed}/{r.total}" for r in results)
    lines = [f"🧪 Проверки Luna: {summary}"]
    failures = [f"{SUITE_NAMES.get(r.name, r.name)}: {f}" for r in results for f in r.failures]
    lines += [f"   ✗ {failure}" for failure in failures[:MAX_FAILURES_SHOWN]]
    if len(failures) > MAX_FAILURES_SHOWN:
        lines.append(f"   и ещё {len(failures) - MAX_FAILURES_SHOWN} — подробности в Langfuse, Datasets")
    return lines


class ModelHealth:
    def __init__(self, langfuse: Langfuse, evaluate: bool = True) -> None:
        self._langfuse = langfuse
        self._evaluate = evaluate

    async def lines(self) -> list[str]:
        """The block for the weekly report; the checks run first so that a broken parser shows up here."""
        if not self._langfuse.enabled:
            return []
        results: list[SuiteResult] = []
        if self._evaluate:
            try:
                results = await run_all("weekly", langfuse=self._langfuse)
            except Exception as exc:  # noqa: BLE001 — the report goes out without the checks rather than not at all
                logger.warning("weekly evaluation failed: %s", type(exc).__name__)
        since = datetime.now(UTC) - WEEK
        generations = await self._langfuse.generations(since)
        if generations is None:
            return ["", "🩺 Langfuse не ответил: здоровье моделей за неделю не собрано.", *eval_lines(results)]
        scores = await self._langfuse.scores("user-feedback", since) or []
        return ["", "🩺 Модели за неделю", *model_lines(generations), feedback_line(scores), *eval_lines(results)]
