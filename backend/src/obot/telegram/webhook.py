"""Webhook transport (production): Telegram pushes updates to /tg/... over HTTPS.

Each bot gets its own secret_token, derived from a server key, so a request
can only be fed to the bot whose secret it carries.
"""

import hashlib
import hmac
import logging
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Update

from obot.telegram.updates import UpdateQueue

log = logging.getLogger(__name__)

FACTORY_PATH = "/tg/factory"
BOT_PATH = "/tg/bot/{tg_bot_id}"


class WebhookManager:
    def __init__(
        self, base_url: str, secret_key: bytes, factory_id: int, updates: UpdateQueue
    ) -> None:
        self._base = base_url.rstrip("/")
        self._key = secret_key
        self._factory_id = factory_id
        self._updates = updates
        self._bots: dict[int, tuple[Bot, Dispatcher]] = {}

    def secret_for(self, tg_bot_id: int) -> str:
        return hmac.new(self._key, f"webhook:{tg_bot_id}".encode(), hashlib.sha256).hexdigest()

    def url_for(self, tg_bot_id: int) -> str:
        path = FACTORY_PATH if tg_bot_id == self._factory_id else BOT_PATH
        return self._base + path.format(tg_bot_id=tg_bot_id)

    def is_running(self, tg_bot_id: int) -> bool:
        return tg_bot_id in self._bots

    async def start(self, bot: Bot, dispatcher: Dispatcher) -> None:
        await self.stop(bot.id, delete_webhook=False)
        await bot.set_webhook(
            url=self.url_for(bot.id),
            secret_token=self.secret_for(bot.id),
            allowed_updates=dispatcher.resolve_used_update_types(),
        )
        self._bots[bot.id] = (bot, dispatcher)

    async def stop(self, tg_bot_id: int, delete_webhook: bool = True) -> None:
        entry = self._bots.pop(tg_bot_id, None)
        if entry is None:
            return
        bot, _ = entry
        if delete_webhook:
            try:
                await bot.delete_webhook()
            except TelegramAPIError:
                # The token may already be revoked (kill switch); requests are refused anyway.
                log.info("could not delete webhook of bot %s", tg_bot_id)
        await bot.session.close()

    async def stop_all(self) -> None:
        # Keep webhooks registered across restarts; Telegram queues updates meanwhile.
        for tg_bot_id in list(self._bots):
            await self.stop(tg_bot_id, delete_webhook=False)

    async def handle(self, tg_bot_id: int, secret: str | None, payload: dict[str, Any]) -> bool:
        """Queue one webhook update; False when the bot is unknown or the secret is wrong."""
        entry = self._bots.get(tg_bot_id)
        if entry is None or not hmac.compare_digest(secret or "", self.secret_for(tg_bot_id)):
            return False
        bot, dispatcher = entry
        update = Update.model_validate(payload, context={"bot": bot})
        await self._updates.submit(bot, dispatcher, update)
        return True
