from dataclasses import dataclass, field

from aiogram import Bot, Dispatcher
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from obot.crypto import TokenCipher
from obot.telegram.polling import PollingManager


@dataclass
class Runtime:
    """Process-wide dependencies shared by the factory and managed-bot handlers."""

    sessionmaker: async_sessionmaker[AsyncSession]
    cipher: TokenCipher
    bots_dispatcher: Dispatcher
    polling: PollingManager = field(default_factory=PollingManager)

    async def launch_bot(self, token: str) -> None:
        await self.polling.start(Bot(token), self.bots_dispatcher)

    async def stop_bot(self, tg_bot_id: int) -> None:
        await self.polling.stop(tg_bot_id)
