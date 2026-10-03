from datetime import datetime, timedelta

from redis.asyncio import Redis

from obot.billing.usage import start_of_day


async def first_today(redis: Redis, key: str, now: datetime, timezone: str) -> bool:
    """True only for the first call with `key` per local day, to avoid repeating notices."""
    day_start = start_of_day(now, timezone)
    expires = day_start + timedelta(days=1)
    full_key = f"notice:{key}:{day_start.date().isoformat()}"
    return bool(await redis.set(full_key, 1, nx=True, exat=int(expires.timestamp())))


async def count_today(redis: Redis, key: str, now: datetime, timezone: str) -> int:
    """Increment and return a per-local-day counter."""
    day_start = start_of_day(now, timezone)
    full_key = f"count:{key}:{day_start.date().isoformat()}"
    async with redis.pipeline(transaction=True) as pipe:
        pipe.incr(full_key)
        pipe.expireat(full_key, int((day_start + timedelta(days=1)).timestamp()))
        value, _ = await pipe.execute()
    return value
