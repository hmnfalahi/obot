from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from aiogram import Bot
from fastapi import FastAPI

from obot.api.deps import ApiState
from obot.api.routes import router
from obot.billing.plans import Pricing
from obot.config import get_settings
from obot.core.llm import ModelRegistry
from obot.crypto import TokenCipher
from obot.db.session import create_engine, create_sessionmaker


async def state_from_settings() -> tuple[ApiState, list]:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    factory = Bot(settings.factory_bot_token.get_secret_value())
    try:
        me = await factory.get_me()
    finally:
        await factory.session.close()
    state = ApiState(
        sessionmaker=create_sessionmaker(engine),
        cipher=TokenCipher.from_b64(settings.token_encryption_key.get_secret_value()),
        pricing=Pricing.load(settings.plans_file),
        models=ModelRegistry(
            settings.openrouter_api_key.get_secret_value(),
            {"cheap": settings.model_cheap, "strong": settings.model_strong},
            settings.max_output_tokens,
        ),
        factory_token=settings.factory_bot_token.get_secret_value(),
        factory_username=me.username,
        public_url=settings.public_base_url or settings.miniapp_url,
    )
    return state, [engine.dispose]


def create_app(state: ApiState | None = None, lifespan=None) -> FastAPI:
    """The API alone (development), or inside obot.server with that process's lifespan."""

    @asynccontextmanager
    async def api_only(app: FastAPI) -> AsyncIterator[None]:
        cleanups = []
        if state is None:
            app.state.api, cleanups = await state_from_settings()
        else:
            app.state.api = state
        yield
        for cleanup in cleanups:
            await cleanup()

    app = FastAPI(title="Obot", lifespan=lifespan or api_only)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(router)
    return app


app = create_app()
