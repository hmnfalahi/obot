from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from obot.bots.service import get_bot_by_tg_id
from obot.db.models import BotStatus
from obot.telegram.runtime import Runtime


class SessionMiddleware(BaseMiddleware):
    """Opens one DB session per update and commits it when the handler succeeds."""

    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with self.runtime.sessionmaker() as session:
            data["session"] = session
            data["runtime"] = self.runtime
            result = await handler(event, data)
            await session.commit()
            return result


class ManagedBotMiddleware(BaseMiddleware):
    """Loads the managed bot record and drops updates for unknown or disabled bots.

    Must run after SessionMiddleware.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        record = await get_bot_by_tg_id(data["session"], data["bot"].id)
        if record is None or record.status != BotStatus.ACTIVE or record.owner.is_disabled:
            return None
        data["bot_record"] = record
        return await handler(event, data)
