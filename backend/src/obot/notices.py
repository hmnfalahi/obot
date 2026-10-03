from datetime import datetime, timedelta

from redis.asyncio import Redis

from obot.billing.usage import start_of_day


async def first_today(redis: Redis, key: str, now: datetime, timezone: str) -> bool:
    """True only for the first call with `key` per local day, to avoid repeating notices."""
    day_start = start_of_day(now, timezone)
    expires = day_start + timedelta(days=1)
    full_key = f"notice:{key}:{day_start.date().isoformat()}"
    return bool(await redis.set(full_key, 1, nx=True, exat=int(expires.timestamp())))
