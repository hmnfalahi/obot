import asyncio
import os
import socket
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from pydantic_ai.models.test import TestModel
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from obot.api import routes
from obot.api.app import create_app
from obot.api.deps import ApiState
from obot.billing.plans import ModelPrice, Pricing
from obot.config import get_settings
from obot.core.byok import Failure, KeyCheckFailed
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
        # The whole schema, not drop_all: tables removed from the models would linger.
        await conn.execute(text("drop schema public cascade"))
        await conn.execute(text("create schema public"))
        await conn.execute(text("create extension if not exists vector"))
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSession(engine, expire_on_commit=False) as s:
        yield s
    await engine.dispose()


@pytest.fixture
def cipher() -> TokenCipher:
    return TokenCipher.from_b64(TokenCipher.generate_key_b64())


# --- mini app API -----------------------------------------------------------------------------

API_FACTORY_TOKEN = "123:factory-secret"
PLANS_FILE = Path(__file__).parent.parent / "plans.yaml"


class FakeByok:
    """Key checks without network: records them, fails with `failure` when set."""

    def __init__(self) -> None:
        self.checked: list[tuple[str, str, str]] = []
        self.failure: Failure | None = None

    async def check(self, provider: str, model: str, api_key: str) -> None:
        self.checked.append((provider, model, api_key))
        if self.failure is not None:
            raise KeyCheckFailed(self.failure)


class FakeModels:
    def __init__(self) -> None:
        self.model = TestModel()
        self.byok = FakeByok()

    def for_tier(self, tier: str):
        return self.model


@pytest.fixture
async def api(session, cipher, monkeypatch):
    async def no_profile_sync(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr(routes, "sync_profile", no_profile_sync)
    access_synced: list[tuple[str, list[int]]] = []

    async def record_access(factory, bot) -> bool:
        access_synced.append((bot.visibility, [u["id"] for u in bot.allowed_users or []]))
        return True

    monkeypatch.setattr(routes, "sync_access", record_access)
    pricing = Pricing.load(PLANS_FILE)
    pricing.models["test"] = ModelPrice(input_per_mtok=1, output_per_mtok=1)
    state = ApiState(
        sessionmaker=async_sessionmaker(session.bind, expire_on_commit=False),
        cipher=cipher,
        pricing=pricing,
        models=FakeModels(),
        factory_token=API_FACTORY_TOKEN,
        factory_username="obotfactorybot",
    )
    app = create_app(state)
    app.state.api = state
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        client.state = state
        client.access_synced = access_synced
        yield client


# --- network -------------------------------------------------------------------------------


@pytest.fixture
async def public_dns(monkeypatch):
    """Resolve every host to a public address so tests need no network."""

    loop = asyncio.get_running_loop()
    real_getaddrinfo = loop.getaddrinfo

    async def fake_getaddrinfo(host, port, **kwargs):
        # The test database and Redis still resolve normally.
        if host in ("localhost", "127.0.0.1", "::1"):
            return await real_getaddrinfo(host, port, **kwargs)
        address = {"internal.test": "10.0.0.5"}.get(host, "93.184.216.34")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)
