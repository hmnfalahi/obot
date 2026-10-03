"""Long-polling transport for many bots in one process (development).

Every update goes through `Dispatcher.feed_update`, the same entry point a
webhook handler uses, so switching to webhooks only replaces this module.
"""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError

log = logging.getLogger(__name__)

POLL_TIMEOUT = 30
MAX_BACKOFF = 30


class PollingManager:
    def __init__(self) -> None:
        self._tasks: dict[int, asyncio.Task[None]] = {}

    def is_running(self, tg_bot_id: int) -> bool:
        return tg_bot_id in self._tasks

    async def start(self, bot: Bot, dispatcher: Dispatcher) -> None:
        """Start polling `bot`, replacing any poller already running for it."""
        await self.stop(bot.id)
        await bot.delete_webhook()
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
        try:
            while True:
                try:
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
                    try:
                        await dispatcher.feed_update(bot, update)
                    except Exception:
                        log.exception(
                            "failed to handle update %s of bot %s", update.update_id, bot.id
                        )
        finally:
            await bot.session.close()
