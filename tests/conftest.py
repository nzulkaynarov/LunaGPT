"""Shared fixtures. Tests never reach the family's database, keys, gateway or Pi.

Before anything from lunagpt is imported, the settings are pointed at test values: every network
address is unroutable and every key is fake, so a test that forgets a mock fails instead of
spending money or touching family data. Locally the database is a throwaway Postgres container;
CI provides its own service database.
"""

import atexit
import os
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

NOWHERE = "http://127.0.0.1:9"
TEST_IMAGE = "pgvector/pgvector:0.8.7-pg17"
os.environ.update(
    {
        "LITELLM_MASTER_KEY": "test-master-key",
        "LITELLM_ASSISTANT_KEY": "test-assistant-key",
        "LANGFUSE_HOST": NOWHERE,
        "LANGFUSE_PUBLIC_KEY": "pk-lf-test",
        "LANGFUSE_SECRET_KEY": "sk-lf-test",
        "LITELLM_ENDPOINT": f"{NOWHERE}/v1/chat/completions",
        "LITELLM_EMBEDDINGS_ENDPOINT": f"{NOWHERE}/v1/embeddings",
        "SEARXNG_URL": NOWHERE,
        "LUNA_PI_API_URL": f"{NOWHERE}/api/luna/v1",
        "LUNA_PI_API_TOKEN": "test-pi-token",
        "LUNA_TELEGRAM_BOT_TOKEN": "",
        "LUNA_GOOGLE_SERVICE_ACCOUNT": "",
    }
)


def _throwaway_postgres() -> str | None:
    """Start a disposable Postgres on a free local port; None when Docker is not available."""
    name = f"lunagpt-test-{uuid.uuid4().hex[:8]}"
    try:
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "--name",
                name,
                "-e",
                "POSTGRES_PASSWORD=test",
                "-p",
                "127.0.0.1::5432",
                "--tmpfs",
                "/var/lib/postgresql/data",
                TEST_IMAGE,
            ],
            check=True,
            capture_output=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    atexit.register(subprocess.run, ["docker", "rm", "-f", name], capture_output=True)
    port = (
        subprocess.run(["docker", "port", name, "5432/tcp"], check=True, capture_output=True, text=True)
        .stdout.split(":")[-1]
        .strip()
    )
    for _ in range(60):  # the image restarts its server once after initdb; wait for TCP
        ready = subprocess.run(
            ["docker", "exec", name, "pg_isready", "-h", "127.0.0.1", "-U", "postgres"], capture_output=True
        )
        if ready.returncode == 0:
            break
        time.sleep(0.5)
    subprocess.run(["docker", "exec", name, "createdb", "-U", "postgres", "lunagpt"], check=True, capture_output=True)
    url = f"postgresql+psycopg://postgres:test@127.0.0.1:{port}/lunagpt"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=True,
        capture_output=True,
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "LUNAGPT_DATABASE_URL": url},
    )
    return url


if not os.environ.get("CI"):
    os.environ["LUNAGPT_DATABASE_URL"] = (
        _throwaway_postgres() or "postgresql+psycopg://postgres:test@127.0.0.1:9/lunagpt"
    )

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from lunagpt.config import settings  # noqa: E402
from lunagpt.db import Base  # noqa: E402
from lunagpt.models import (  # noqa: E402
    AssistantApplication,
    AssistantCatPhrase,
    AssistantCatStory,
    AssistantFamilyList,
    AssistantFamilyListItem,
    AssistantFamilyMemory,
    AssistantMember,
    AssistantMemory,
    AssistantMessage,
    AssistantReminder,
    AssistantSchedule,
    AssistantTask,
    AssistantTimeRequest,
    AssistantUsage,
)

TABLES = [
    model.__table__
    for model in (
        AssistantMember,
        AssistantApplication,
        AssistantMemory,
        AssistantFamilyMemory,
        AssistantMessage,
        AssistantTask,
        AssistantReminder,
        AssistantSchedule,
        AssistantCatPhrase,
        AssistantCatStory,
        AssistantFamilyList,
        AssistantFamilyListItem,
        AssistantTimeRequest,
        AssistantUsage,
    )
]


@pytest_asyncio.fixture
async def db_sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    url = make_url(settings.lunagpt_database_url)
    name = f"luna_test_{uuid.uuid4().hex[:10]}"
    admin = create_async_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{name}"'))
    except OperationalError:
        await admin.dispose()
        if os.environ.get("CI"):
            raise
        pytest.skip("Postgres is not reachable")
    engine = create_async_engine(url.set(database=name))
    async with engine.begin() as connection:
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await connection.run_sync(Base.metadata.create_all, tables=TABLES)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()
    async with admin.connect() as connection:
        await connection.execute(text(f'DROP DATABASE "{name}"'))
    await admin.dispose()
