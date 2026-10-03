"""Run the factory bot and all active managed bots with long polling.

cd backend && uv run python -m obot.telegram.run
"""

import asyncio
import logging

from aiogram import Bot, Dispatcher

from obot.bots.service import list_active_bots
from obot.config import get_settings
from obot.crypto import TokenCipher
from obot.db.session import create_engine, create_sessionmaker
from obot.telegram import bots, factory
from obot.telegram.middleware import ManagedBotMiddleware, SessionMiddleware
from obot.telegram.runtime import Runtime

log = logging.getLogger(__name__)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    if settings.factory_bot_token is None or settings.token_encryption_key is None:
        raise SystemExit("Set OBOT_FACTORY_BOT_TOKEN and OBOT_TOKEN_ENCRYPTION_KEY in backend/.env")

    engine = create_engine(settings.database_url)
    runtime = Runtime(
        sessionmaker=create_sessionmaker(engine),
        cipher=TokenCipher.from_b64(settings.token_encryption_key.get_secret_value()),
        bots_dispatcher=Dispatcher(),
    )

    runtime.bots_dispatcher.update.outer_middleware(SessionMiddleware(runtime))
    runtime.bots_dispatcher.update.middleware(ManagedBotMiddleware())
    runtime.bots_dispatcher.include_router(bots.router)

    factory_dispatcher = Dispatcher()
    factory_dispatcher.update.outer_middleware(SessionMiddleware(runtime))
    factory_dispatcher.include_router(factory.router)

    factory_bot = Bot(settings.factory_bot_token.get_secret_value())
    me = await factory_bot.get_me()
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
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
