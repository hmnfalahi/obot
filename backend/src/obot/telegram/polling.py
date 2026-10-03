"""Long-polling transport for many bots in one process (development)."""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError

from obot.telegram.updates import UpdateQueue

log = logging.getLogger(__name__)

POLL_TIMEOUT = 30
MAX_BACKOFF = 30


class PollingManager:
    def __init__(self, updates: UpdateQueue) -> None:
        self._updates = updates
        self._tasks: dict[int, asyncio.Task[None]] = {}

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
                    await self._updates.submit(bot, dispatcher, update)
        finally:
            await bot.session.close()
