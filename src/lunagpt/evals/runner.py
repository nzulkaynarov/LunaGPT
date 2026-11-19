"""Run the suites through the live gateway and record every case in Langfuse as a dataset run.

Each case becomes a trace with the model calls, a score "eval-correct" (1 or 0) with the reason, and a link from
the dataset item, so Langfuse shows every run of a suite side by side. The weekly report reads the scores back."""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field

from lunagpt.evals.suites import SUITES, Suite, run_name
from lunagpt.langfuse_api import Langfuse

logger = logging.getLogger("lunagpt.evals")

SCORE_NAME = "eval-correct"
CLIENT_TAG = "client:luna-eval"
PARALLEL = 4  # the local model serves one request at a time; more only queues


@dataclass
class SuiteResult:
    name: str
    passed: int = 0
    total: int = 0
    failures: list[str] = field(default_factory=list)


def dataset_name(suite: Suite) -> str:
    return f"luna-{suite.name}"


async def run_suite(suite: Suite, langfuse: Langfuse, run: str) -> SuiteResult:
    result = SuiteResult(suite.name, total=len(suite.cases))
    await langfuse.ensure_dataset(dataset_name(suite), suite.description)
    gate = asyncio.Semaphore(PARALLEL)

    async def one(case_index: int) -> None:
        case = suite.cases[case_index]
        item_id = f"{suite.name}-{case.id}"
        trace_id = str(uuid.uuid4())
        tags = [CLIENT_TAG, f"suite:{suite.name}", f"run:{run}"]
        metadata = {
            "trace_id": trace_id,
            "user_id": "eval",
            "session_id": f"eval:{run}",
            "source": "luna_eval",
            "tags": tags,
        }
        async with gate:
            try:
                output = await suite.task(case.input, metadata)
                passed, note = suite.check(output, case.expected)
            except Exception as exc:  # noqa: BLE001 — a crashing case is a failed case, not a failed run
                output, passed, note = None, False, f"error: {type(exc).__name__}: {exc}"
        if passed:
            result.passed += 1
        else:
            result.failures.append(f"{case.id}: {note}")
        await langfuse.upsert_item(dataset_name(suite), item_id, case.input, case.expected)
        await langfuse.trace(trace_id, f"eval-{suite.name}", case.input, output, tags)
        await langfuse.score(f"{trace_id}-correct", trace_id, SCORE_NAME, 1 if passed else 0, note or None)
        await langfuse.link_run_item(run, item_id, trace_id, {"passed": passed})

    await asyncio.gather(*(one(index) for index in range(len(suite.cases))))
    logger.info("eval %s: %d/%d", suite.name, result.passed, result.total)
    return result


async def run_all(source: str, names: list[str] | None = None, langfuse: Langfuse | None = None) -> list[SuiteResult]:
    langfuse = langfuse or Langfuse()
    run = run_name(source)
    chosen = [suite for suite in SUITES if not names or suite.name in names]
    return [await run_suite(suite, langfuse, run) for suite in chosen]
