"""Run the factory bot and all active managed bots with long polling.

cd backend && uv run python -m obot.telegram.run
"""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramNetworkError
from aiogram.types import User
from redis.asyncio import Redis

from obot.billing.plans import Pricing
from obot.bots.service import list_active_bots
from obot.config import get_settings
from obot.core.llm import ModelRegistry
from obot.crypto import TokenCipher
from obot.db.session import create_engine, create_sessionmaker
from obot.observability import setup_tracing
from obot.telegram import bots, factory
from obot.telegram.middleware import ManagedBotMiddleware, SessionMiddleware
from obot.telegram.runtime import Runtime

log = logging.getLogger(__name__)


async def get_me_with_retry(bot: Bot, attempts: int = 6) -> User:
    for attempt in range(attempts):
        try:
            return await bot.get_me()
        except TelegramNetworkError as e:
            if attempt == attempts - 1:
                raise
            delay = 2**attempt
            log.warning("cannot reach Telegram (%s); retrying in %ss", e, delay)
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    required = ("factory_bot_token", "token_encryption_key", "openrouter_api_key")
    if missing := [name for name in required if getattr(settings, name) is None]:
        env_names = ", ".join(f"OBOT_{name.upper()}" for name in missing)
        raise SystemExit(f"Set {env_names} in backend/.env")

    pricing = Pricing.load(settings.plans_file)
    pricing.require_models([settings.model_cheap, settings.model_strong])
    setup_tracing(settings)

    engine = create_engine(settings.database_url)
    redis = Redis.from_url(settings.redis_url)
    factory_bot = Bot(settings.factory_bot_token.get_secret_value())
    runtime = Runtime(
        sessionmaker=create_sessionmaker(engine),
        cipher=TokenCipher.from_b64(settings.token_encryption_key.get_secret_value()),
        bots_dispatcher=Dispatcher(),
        models=ModelRegistry(
            settings.openrouter_api_key.get_secret_value(),
            {"cheap": settings.model_cheap, "strong": settings.model_strong},
            settings.max_output_tokens,
        ),
        history_limit=settings.history_limit,
        pricing=pricing,
        redis=redis,
        factory_bot=factory_bot,
    )

    runtime.bots_dispatcher.update.outer_middleware(SessionMiddleware(runtime))
    runtime.bots_dispatcher.update.middleware(ManagedBotMiddleware())
    runtime.bots_dispatcher.include_router(bots.router)

    factory_dispatcher = Dispatcher()
    factory_dispatcher.update.outer_middleware(SessionMiddleware(runtime))
    factory_dispatcher.include_router(factory.router)

    me = await get_me_with_retry(factory_bot)
    log.info("factory bot @%s", me.username)

    async with runtime.sessionmaker() as session:
        records = await list_active_bots(session)
    for record in records:
        token = runtime.cipher.decrypt(record.token_encrypted, str(record.tg_bot_id))
        await runtime.launch_bot(token)
    log.info("launched %d managed bots", len(records))

    await runtime.polling.start(factory_bot, factory_dispatcher)
    try:
        await asyncio.Event().wait()
    finally:
        await runtime.polling.stop_all()
        await redis.aclose()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
