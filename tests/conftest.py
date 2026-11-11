"""Shared fixtures: a real Postgres database created for each test that needs one."""

import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from lunagpt.config import settings
from lunagpt.db import Base
from lunagpt.models import (
    AssistantApplication,
    AssistantFamilyMemory,
    AssistantMember,
    AssistantMemory,
    AssistantMessage,
    AssistantReminder,
    AssistantSchedule,
    AssistantTask,
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
