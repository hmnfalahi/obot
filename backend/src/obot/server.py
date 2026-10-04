"""Production process: mini app API, Telegram webhooks and background jobs.

    uvicorn obot.server:app --host 0.0.0.0 --port 8000

Run a single instance: background jobs and the bot transport live in-process.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Header, HTTPException, Request, Response

from obot.api.app import create_app
from obot.api.deps import ApiState
from obot.config import get_settings
from obot.telegram.bootstrap import Services, start_services, stop_services
from obot.telegram.webhook import BOT_PATH, FACTORY_PATH

log = logging.getLogger(__name__)

SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"


def init_sentry(dsn: str | None) -> None:
    if not dsn:
        return
    import sentry_sdk

    sentry_sdk.init(dsn=dsn, traces_sample_rate=0.0, send_default_pii=False)


@asynccontextmanager
async def lifespan(app) -> AsyncIterator[None]:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    init_sentry(settings.sentry_dsn)
    services = await start_services(settings, settings.update_mode)
    runtime = services.runtime
    app.state.services = services
    app.state.api = ApiState(
        sessionmaker=runtime.sessionmaker,
        cipher=runtime.cipher,
        pricing=runtime.pricing,
        models=runtime.models,
        factory_token=settings.factory_bot_token.get_secret_value(),
        factory_username=services.factory_username,
    )
    yield
    await stop_services(services)


app = create_app(lifespan=lifespan)


async def _feed(request: Request, tg_bot_id: int, secret: str | None) -> Response:
    services: Services = request.app.state.services
    webhooks = services.webhooks
    if webhooks is None:
        raise HTTPException(404)
    payload: dict[str, Any] = await request.json()
    if not await webhooks.handle(tg_bot_id, secret, payload):
        # Unknown bot or wrong secret; also stops Telegram retrying for a removed bot.
        raise HTTPException(403)
    return Response(status_code=200)


@app.post(FACTORY_PATH, include_in_schema=False)
async def factory_webhook(
    request: Request, secret: Annotated[str | None, Header(alias=SECRET_HEADER)] = None
) -> Response:
    return await _feed(request, request.app.state.services.factory_bot.id, secret)


@app.post(BOT_PATH, include_in_schema=False)
async def bot_webhook(
    tg_bot_id: int,
    request: Request,
    secret: Annotated[str | None, Header(alias=SECRET_HEADER)] = None,
) -> Response:
    return await _feed(request, tg_bot_id, secret)
