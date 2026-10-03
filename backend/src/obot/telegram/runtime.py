from dataclasses import dataclass, field

import httpx
from aiogram import Bot, Dispatcher
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from obot.billing.plans import Pricing
from obot.core.llm import ModelRegistry
from obot.core.memory import Embedder
from obot.crypto import TokenCipher
from obot.telegram.polling import PollingManager
from obot.telegram.sender import Sender


@dataclass
class Runtime:
    """Process-wide dependencies shared by the factory and managed-bot handlers."""

    sessionmaker: async_sessionmaker[AsyncSession]
    cipher: TokenCipher
    bots_dispatcher: Dispatcher
    models: ModelRegistry
    history_limit: int
    pricing: Pricing
    redis: Redis
    # Used to notify owners (they have all started the factory bot).
    factory_bot: Bot
    factory_daily_messages: int
    # Shared client for tools (web search, page fetches)
    http: httpx.AsyncClient
    tavily_api_key: str | None
    sender: Sender
    embedder: Embedder
    miniapp_url: str | None = None
    admins: frozenset[int] = frozenset()
    # Abuse reports one person may send per day
    daily_reports: int = 5

    def panel_url(self, bot_id: int | None = None) -> str | None:
        if not self.miniapp_url:
            return None
        base = self.miniapp_url.rstrip("/")
        return f"{base}/?bot={bot_id}" if bot_id else f"{base}/"

    polling: PollingManager = field(default_factory=PollingManager)

    async def launch_bot(self, token: str) -> None:
        await self.polling.start(Bot(token), self.bots_dispatcher)

    async def stop_bot(self, tg_bot_id: int) -> None:
        await self.polling.stop(tg_bot_id)
