import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from obot.config import get_settings
from obot.crypto import TokenCipher
from obot.db.models import Base

TEST_DATABASE_URL = os.environ.get(
    "OBOT_TEST_DATABASE_URL",
    make_url(get_settings().database_url).set(database="obot_test").render_as_string(False),
)


async def _ensure_test_database() -> None:
    url = make_url(TEST_DATABASE_URL)
    admin = create_async_engine(
        url.set(database="postgres"), poolclass=NullPool, isolation_level="AUTOCOMMIT"
    )
    async with admin.connect() as conn:
        exists = await conn.scalar(
            text("select 1 from pg_database where datname = :name"), {"name": url.database}
        )
        if not exists:
            await conn.execute(text(f'create database "{url.database}"'))
    await admin.dispose()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    await _ensure_test_database()
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(text("create extension if not exists vector"))
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSession(engine, expire_on_commit=False) as s:
        yield s
    await engine.dispose()


@pytest.fixture
def cipher() -> TokenCipher:
    return TokenCipher.from_b64(TokenCipher.generate_key_b64())
