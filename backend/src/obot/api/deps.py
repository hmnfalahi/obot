from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import joinedload

from obot.api.auth import InvalidInitData, validate_init_data
from obot.billing.plans import Pricing
from obot.bots.access import can_use
from obot.bots.service import get_or_create_user
from obot.core.llm import ModelRegistry
from obot.crypto import TokenCipher
from obot.db.models import ApiEndpoint, Bot, BotStatus, Task, User


@dataclass
class ApiState:
    sessionmaker: async_sessionmaker[AsyncSession]
    cipher: TokenCipher
    pricing: Pricing
    models: ModelRegistry
    factory_token: str
    factory_username: str


def get_state(request: Request) -> ApiState:
    return request.app.state.api


State = Annotated[ApiState, Depends(get_state)]


async def get_session(state: State) -> AsyncIterator[AsyncSession]:
    async with state.sessionmaker() as session:
        yield session
        await session.commit()


Session = Annotated[AsyncSession, Depends(get_session)]


async def current_user(
    state: State,
    session: Session,
    authorization: Annotated[str | None, Header()] = None,
) -> User:
    """Authenticate with `Authorization: tma <initData>` from Telegram.WebApp."""
    scheme, _, init_data = (authorization or "").partition(" ")
    if scheme.lower() != "tma" or not init_data:
        raise HTTPException(401, "missing Telegram init data")
    try:
        tg_user = validate_init_data(init_data, state.factory_token)
    except InvalidInitData as e:
        raise HTTPException(401, f"invalid Telegram init data: {e}") from e
    user = await get_or_create_user(session, tg_user.id, tg_user.language_code)
    if user.is_disabled:
        raise HTTPException(403, "account disabled")
    return user


CurrentUser = Annotated[User, Depends(current_user)]


async def owned_bot(bot_id: int, user: CurrentUser, session: Session) -> Bot:
    bot = await session.get(Bot, bot_id, options=[joinedload(Bot.owner)])
    # 404 rather than 403: do not reveal that other users' bots exist.
    if bot is None or bot.owner_id != user.id:
        raise HTTPException(404, "bot not found")
    return bot


OwnedBot = Annotated[Bot, Depends(owned_bot)]


async def usable_bot(bot_id: int, user: CurrentUser, session: Session) -> Bot:
    """An active bot the user may use (its owner always may); 404 otherwise."""
    bot = await session.get(Bot, bot_id, options=[joinedload(Bot.owner)])
    if bot is None or bot.status != BotStatus.ACTIVE or not can_use(bot, user.tg_user_id):
        raise HTTPException(404, "bot not found")
    return bot


UsableBot = Annotated[Bot, Depends(usable_bot)]


async def owned_task(task_id: int, user: CurrentUser, session: Session) -> Task:
    """One of the user's own tasks; the bot's owner has no say over them (SPEC 8.7)."""
    task = await session.get(Task, task_id)
    if task is None or task.user_id != user.id:
        raise HTTPException(404, "task not found")
    return task


OwnedTask = Annotated[Task, Depends(owned_task)]


async def owned_api(api_id: int, user: CurrentUser, session: Session) -> ApiEndpoint:
    row = await session.get(ApiEndpoint, api_id)
    bot = await session.get(Bot, row.bot_id, options=[joinedload(Bot.owner)]) if row else None
    if row is None or bot is None or bot.owner_id != user.id:
        raise HTTPException(404, "api not found")
    return row


OwnedApi = Annotated[ApiEndpoint, Depends(owned_api)]
