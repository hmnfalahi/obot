"""The single path for outgoing messages of managed bots.

Applies a per-chat rate limit shared through Redis (Telegram allows about 20
messages a minute in a group) and honours flood-control retry_after.
"""

import asyncio
import logging
import time

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import Message, ReplyParameters
from redis.asyncio import Redis

log = logging.getLogger(__name__)

TELEGRAM_TEXT_LIMIT = 4096
GROUP_PER_MINUTE = 20
PRIVATE_PER_MINUTE = 60
MAX_FLOOD_RETRIES = 3


def split_text(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    """Split on paragraph/line/space boundaries so each chunk fits one Telegram message."""
    chunks: list[str] = []
    while len(text) > limit:
        cut = max(text.rfind(sep, 0, limit) for sep in ("\n\n", "\n", " "))
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks


class Sender:
    def __init__(
        self,
        redis: Redis,
        group_per_minute: int = GROUP_PER_MINUTE,
        private_per_minute: int = PRIVATE_PER_MINUTE,
    ) -> None:
        self._redis = redis
        self._group_limit = group_per_minute
        self._private_limit = private_per_minute

    async def send_text(
        self, bot: Bot, chat_id: int, text: str, *, group: bool, reply_to: int | None = None
    ) -> list[Message]:
        sent = []
        for i, chunk in enumerate(split_text(text)):
            await self.acquire(bot.id, chat_id, group)
            reply = (
                ReplyParameters(message_id=reply_to, allow_sending_without_reply=True)
                if reply_to and i == 0
                else None
            )
            sent.append(await self._send(bot, chat_id, chunk, reply))
        return sent

    async def acquire(self, bot_id: int, chat_id: int, group: bool) -> None:
        """Wait until the chat's fixed one-minute window has room for one more message."""
        limit = self._group_limit if group else self._private_limit
        while True:
            now = time.time()
            window = int(now // 60)
            key = f"rate:{bot_id}:{chat_id}:{window}"
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.incr(key)
                pipe.expire(key, 61)
                count, _ = await pipe.execute()
            if count <= limit:
                return
            wait = (window + 1) * 60 - now
            log.info("rate limit for bot %s chat %s; waiting %.1fs", bot_id, chat_id, wait)
            await asyncio.sleep(wait)

    async def _send(
        self, bot: Bot, chat_id: int, text: str, reply: ReplyParameters | None
    ) -> Message:
        for attempt in range(MAX_FLOOD_RETRIES + 1):
            try:
                return await bot.send_message(chat_id, text, reply_parameters=reply)
            except TelegramRetryAfter as e:
                if attempt == MAX_FLOOD_RETRIES:
                    raise
                log.warning("flood control in chat %s; retrying in %ss", chat_id, e.retry_after)
                await asyncio.sleep(e.retry_after)
        raise AssertionError("unreachable")
