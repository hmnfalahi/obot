from datetime import UTC, datetime, timedelta

import pytest
from redis.asyncio import Redis

from obot.config import get_settings
from obot.notices import first_today

# Dedicated logical database so tests never touch dev data.
TEST_REDIS_DB = 15


@pytest.fixture
async def redis():
    client = Redis.from_url(get_settings().redis_url, db=TEST_REDIS_DB)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


async def test_first_today_only_once_per_local_day(redis) -> None:
    # Noon tomorrow: keys expire at the end of that day, which must still be in the future.
    now = (datetime.now(UTC) + timedelta(days=1)).replace(hour=12, minute=0)

    assert await first_today(redis, "k", now, "UTC")
    assert not await first_today(redis, "k", now + timedelta(hours=1), "UTC")
    assert await first_today(redis, "other", now, "UTC")
    assert await first_today(redis, "k", now + timedelta(days=1), "UTC")
