import asyncio

from obot.telegram.concurrency import KeyedLock


async def test_same_key_runs_in_order_other_keys_concurrently() -> None:
    lock = KeyedLock()
    events: list[str] = []

    async def job(key: str, name: str, delay: float) -> None:
        async with lock.hold(key):
            events.append(f"start {name}")
            await asyncio.sleep(delay)
            events.append(f"end {name}")

    await asyncio.gather(job("a", "a1", 0.05), job("a", "a2", 0), job("b", "b1", 0))

    assert events.index("end a1") < events.index("start a2")
    assert events.index("end b1") < events.index("end a1")
    assert len(lock) == 0
