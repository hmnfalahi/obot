import asyncio
import logging
from collections.abc import Awaitable, Callable, Hashable

from aiogram import Bot, Dispatcher
from aiogram.types import Update

from obot.telegram.concurrency import KeyedLock, ordering_key

log = logging.getLogger(__name__)

# Upper bound of updates handled at the same time across all bots.
MAX_CONCURRENT_UPDATES = 64


class UpdateQueue:
    """Handles updates concurrently across chats and in order within one chat.

    Shared by the polling and webhook transports.
    """

    def __init__(self, max_concurrent: int = MAX_CONCURRENT_UPDATES) -> None:
        self._tasks: set[asyncio.Task[None]] = set()
        self._order = KeyedLock()
        self._slots = asyncio.Semaphore(max_concurrent)

    async def submit(self, bot: Bot, dispatcher: Dispatcher, update: Update) -> None:
        """Wait for a free slot, then handle `update` in the background."""
        await self._slots.acquire()
        # Tasks start in creation order and queue on their chat's FIFO lock before
        # their first await, so one chat's updates keep their order.
        task = asyncio.create_task(self._handle(bot, dispatcher, update))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def spawn(self, tg_bot_id: int, key: Hashable, job: Callable[[], Awaitable[None]]) -> None:
        """Run work for a bot (not from a Telegram update) in order with its chat's updates."""
        task = asyncio.create_task(self._run_job(tg_bot_id, key, job))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_job(
        self, tg_bot_id: int, key: Hashable, job: Callable[[], Awaitable[None]]
    ) -> None:
        # The slot is taken inside the task: the caller may itself hold one.
        async with self._slots:
            try:
                async with self._order.hold((tg_bot_id, key)):
                    await job()
            except Exception:
                log.exception("background job for bot %s failed", tg_bot_id)

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _handle(self, bot: Bot, dispatcher: Dispatcher, update: Update) -> None:
        try:
            async with self._order.hold((bot.id, ordering_key(update))):
                await dispatcher.feed_update(bot, update)
        except Exception:
            log.exception("failed to handle update %s of bot %s", update.update_id, bot.id)
        finally:
            self._slots.release()
