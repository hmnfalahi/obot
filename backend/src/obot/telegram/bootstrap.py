"""Builds and starts everything a bot process needs: runtime, transport, bots, jobs.

Used by obot.telegram.run (polling, development) and obot.server (webhooks).
"""

import asyncio
import base64
import logging
from dataclasses import dataclass, field
from typing import Literal

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError
from aiogram.types import MenuButtonDefault, MenuButtonWebApp, User, WebAppInfo
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from obot.billing.plans import Pricing
from obot.bots.service import list_active_bots
from obot.config import Settings
from obot.core.fetch import make_client
from obot.core.llm import ModelRegistry
from obot.core.memory import Embedder
from obot.crypto import TokenCipher
from obot.db.session import create_engine, create_sessionmaker
from obot.jobs import run_every
from obot.memory.jobs import extract_pending, purge_history
from obot.observability import setup_tracing
from obot.telegram import bots, factory, moderation, payments, scheduling
from obot.telegram.middleware import ManagedBotMiddleware, SessionMiddleware
from obot.telegram.polling import PollingManager
from obot.telegram.runtime import Runtime
from obot.telegram.sender import Sender
from obot.telegram.task_runner import run_due_tasks
from obot.telegram.updates import UpdateQueue
from obot.telegram.webhook import WebhookManager

log = logging.getLogger(__name__)

Mode = Literal["polling", "webhook"]
REQUIRED_SETTINGS = ("factory_bot_token", "token_encryption_key", "openrouter_api_key")


@dataclass
class Services:
    runtime: Runtime
    factory_bot: Bot
    factory_username: str
    factory_dispatcher: Dispatcher
    engine: AsyncEngine
    updates: UpdateQueue
    jobs: list[asyncio.Task] = field(default_factory=list)

    @property
    def webhooks(self) -> WebhookManager | None:
        transport = self.runtime.transport
        return transport if isinstance(transport, WebhookManager) else None


def check_settings(settings: Settings, mode: Mode) -> None:
    required = REQUIRED_SETTINGS + (("public_base_url",) if mode == "webhook" else ())
    if missing := [name for name in required if not getattr(settings, name)]:
        env_names = ", ".join(f"OBOT_{name.upper()}" for name in missing)
        raise SystemExit(f"Set {env_names}")


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


async def setup_panel_button(bot: Bot, runtime: Runtime) -> None:
    """Point the factory's menu button at the mini app, or reset it when there is none."""
    url = runtime.panel_url()
    try:
        if url:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="Panel", web_app=WebAppInfo(url=url))
            )
        else:
            await bot.set_chat_menu_button(menu_button=MenuButtonDefault())
    except TelegramAPIError:
        log.warning("could not set the factory menu button", exc_info=True)


async def start_services(settings: Settings, mode: Mode) -> Services:
    check_settings(settings, mode)
    pricing = Pricing.load(settings.plans_file)
    pricing.require_models([settings.model_cheap, settings.model_strong, settings.embedding_model])
    setup_tracing(settings)

    engine = create_engine(settings.database_url)
    models = ModelRegistry(
        settings.openrouter_api_key.get_secret_value(),
        {"cheap": settings.model_cheap, "strong": settings.model_strong},
        settings.max_output_tokens,
    )
    redis = Redis.from_url(settings.redis_url)
    factory_bot = Bot(settings.factory_bot_token.get_secret_value())
    encryption_key = settings.token_encryption_key.get_secret_value()
    runtime = Runtime(
        sessionmaker=create_sessionmaker(engine),
        cipher=TokenCipher.from_b64(encryption_key),
        bots_dispatcher=Dispatcher(),
        models=models,
        history_limit=settings.history_limit,
        pricing=pricing,
        redis=redis,
        factory_bot=factory_bot,
        factory_daily_messages=settings.factory_daily_messages,
        http=make_client(),
        sender=Sender(redis),
        embedder=Embedder(models.client, settings.embedding_model),
        miniapp_url=settings.miniapp_url,
        admins=settings.admins,
        manual_payment_text=settings.manual_payment_text,
        tavily_api_key=(
            settings.tavily_api_key.get_secret_value() if settings.tavily_api_key else None
        ),
    )

    runtime.bots_dispatcher.update.outer_middleware(SessionMiddleware(runtime))
    runtime.bots_dispatcher.update.middleware(ManagedBotMiddleware())
    # Before bots: /tasks must not reach the catch-all chat handler.
    runtime.bots_dispatcher.include_router(scheduling.router)
    runtime.bots_dispatcher.include_router(bots.router)

    factory_dispatcher = Dispatcher()
    factory_dispatcher.update.outer_middleware(SessionMiddleware(runtime))
    # Moderation first: /report and admin commands must not reach the designer.
    factory_dispatcher.include_router(moderation.router)
    factory_dispatcher.include_router(payments.router)
    factory_dispatcher.include_router(factory.router)

    me = await get_me_with_retry(factory_bot)
    log.info("factory bot @%s, %s mode", me.username, mode)
    runtime.factory_username = me.username

    updates = UpdateQueue()
    runtime.updates = updates
    if mode == "webhook":
        # Webhook secrets derive from the encryption key, so they never need storing.
        runtime.transport = WebhookManager(
            settings.public_base_url, base64.urlsafe_b64decode(encryption_key), me.id, updates
        )
    else:
        runtime.transport = PollingManager(updates)

    async with runtime.sessionmaker() as session:
        records = await list_active_bots(session)
    for record in records:
        try:
            await runtime.launch_bot(
                runtime.cipher.decrypt(record.token_encrypted, str(record.tg_bot_id))
            )
        except TelegramAPIError:
            log.exception("could not launch bot %s", record.id)
    log.info("launched %d managed bots", len(records))

    await setup_panel_button(factory_bot, runtime)
    await runtime.transport.start(factory_bot, factory_dispatcher)

    services = Services(runtime, factory_bot, me.username, factory_dispatcher, engine, updates)
    services.jobs = [
        asyncio.create_task(run_every("scheduled-tasks", 60, lambda: run_due_tasks(runtime))),
        asyncio.create_task(
            run_every(
                "memory-extraction",
                settings.memory_extract_interval_seconds,
                lambda: extract_pending(
                    runtime.sessionmaker, pricing, models.for_tier("cheap"), runtime.embedder
                ),
            )
        ),
        asyncio.create_task(
            run_every(
                "history-retention",
                6 * 3600,
                lambda: purge_history(runtime.sessionmaker, settings.message_retention_days),
            )
        ),
    ]
    return services


async def stop_services(services: Services) -> None:
    for job in services.jobs:
        job.cancel()
    await asyncio.gather(*services.jobs, return_exceptions=True)
    await services.runtime.transport.stop_all()
    await services.updates.drain()
    await services.runtime.http.aclose()
    await services.runtime.redis.aclose()
    await services.engine.dispose()
