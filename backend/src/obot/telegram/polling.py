"""Long-polling transport for many bots in one process (development).

Every update goes through `Dispatcher.feed_update`, the same entry point a
webhook handler uses, so switching to webhooks only replaces this module.
Updates of different chats are handled concurrently, those of one chat in order.
"""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.types import Update

from obot.telegram.concurrency import KeyedLock, ordering_key

log = logging.getLogger(__name__)

POLL_TIMEOUT = 30
MAX_BACKOFF = 30
# Upper bound of updates handled at the same time across all bots.
MAX_CONCURRENT_UPDATES = 64


class PollingManager:
    def __init__(self) -> None:
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._handlers: set[asyncio.Task[None]] = set()
        self._order = KeyedLock()
        self._slots = asyncio.Semaphore(MAX_CONCURRENT_UPDATES)

    def is_running(self, tg_bot_id: int) -> bool:
        return tg_bot_id in self._tasks

    async def start(self, bot: Bot, dispatcher: Dispatcher) -> None:
        """Start polling `bot`, replacing any poller already running for it."""
        await self.stop(bot.id)
        self._tasks[bot.id] = asyncio.create_task(
            self._poll(bot, dispatcher), name=f"poll-{bot.id}"
        )

    async def stop(self, tg_bot_id: int) -> None:
        task = self._tasks.pop(tg_bot_id, None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def stop_all(self) -> None:
        for tg_bot_id in list(self._tasks):
            await self.stop(tg_bot_id)

    async def _poll(self, bot: Bot, dispatcher: Dispatcher) -> None:
        allowed = dispatcher.resolve_used_update_types()
        offset: int | None = None
        backoff = 1
        webhook_cleared = False
        try:
            while True:
                try:
                    if not webhook_cleared:
                        # getUpdates fails while a webhook is set.
                        await bot.delete_webhook()
                        webhook_cleared = True
                    updates = await bot.get_updates(
                        offset=offset, timeout=POLL_TIMEOUT, allowed_updates=allowed
                    )
                except TelegramUnauthorizedError:
                    log.warning("token of bot %s was revoked; stopping its poller", bot.id)
                    self._tasks.pop(bot.id, None)
                    return
                except TelegramNetworkError as e:
                    log.warning("polling bot %s failed: %s; retrying in %ss", bot.id, e, backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, MAX_BACKOFF)
                    continue
                backoff = 1
                for update in updates:
                    offset = update.update_id + 1
                    # Tasks start in creation order and queue on their chat's FIFO lock
                    # before their first await, so one chat's updates keep their order.
                    await self._slots.acquire()
                    task = asyncio.create_task(self._handle(bot, dispatcher, update))
                    self._handlers.add(task)
                    task.add_done_callback(self._handlers.discard)
        finally:
            await bot.session.close()

    async def _handle(self, bot: Bot, dispatcher: Dispatcher, update: Update) -> None:
        try:
            async with self._order.hold((bot.id, ordering_key(update))):
                await dispatcher.feed_update(bot, update)
        except Exception:
            log.exception("failed to handle update %s of bot %s", update.update_id, bot.id)
        finally:
            self._slots.release()
