import asyncio
import logging
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)


async def run_every(name: str, seconds: float, job: Callable[[], Awaitable[object]]) -> None:
    """Run `job` forever with a fixed pause; failures are logged and never stop the loop.

    In-process for now; moves to the Taskiq worker together with scheduled tasks.
    """
    while True:
        try:
            await job()
        except Exception:
            log.exception("periodic job %s failed", name)
        await asyncio.sleep(seconds)
