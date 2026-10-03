import asyncio
from collections.abc import AsyncIterator, Hashable
from contextlib import asynccontextmanager

from aiogram.types import Update


class KeyedLock:
    """One asyncio lock per key, dropped once nobody holds or waits for it."""

    def __init__(self) -> None:
        self._locks: dict[Hashable, asyncio.Lock] = {}
        self._users: dict[Hashable, int] = {}

    @asynccontextmanager
    async def hold(self, key: Hashable) -> AsyncIterator[None]:
        lock = self._locks.setdefault(key, asyncio.Lock())
        self._users[key] = self._users.get(key, 0) + 1
        try:
            async with lock:
                yield
        finally:
            self._users[key] -= 1
            if self._users[key] == 0:
                del self._users[key]
                del self._locks[key]

    def __len__(self) -> int:
        return len(self._locks)


def ordering_key(update: Update) -> Hashable:
    """Updates with the same key are handled in order; different keys run concurrently."""
    if update.message:
        return ("chat", update.message.chat.id)
    if update.my_chat_member:
        return ("chat", update.my_chat_member.chat.id)
    if update.callback_query:
        return ("user", update.callback_query.from_user.id)
    if update.managed_bot:
        # Creation and later token/owner changes of one bot must not interleave.
        return ("managed_bot", update.managed_bot.bot_user.id)
    return ("update", update.update_id)
