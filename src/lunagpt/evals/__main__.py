"""python -m lunagpt.evals [suite ...]: run the evaluation suites now and print the result.

Inside the container: docker exec luna-assistant python -m lunagpt.evals
From the host, against the working tree: make eval (points the run at the published gateway and Langfuse)."""

import asyncio
import logging
import sys

from lunagpt.evals.runner import run_all


async def main(names: list[str]) -> int:
    results = await run_all("manual", names or None)
    failed = False
    for result in results:
        print(f"{result.name}: {result.passed}/{result.total}")
        for failure in result.failures:
            print(f"  ✗ {failure}")
        failed = failed or result.passed < result.total
    return 1 if failed else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    sys.exit(asyncio.run(main(sys.argv[1:])))
