"""People who use a bot: whoever started it in private (SPEC 8.7).

Starting a bot creates the person's Obot account (free plan) if needed; they
never have to visit the factory. Blocking the bot ends their use of it, and
starting it again resumes it.
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from obot.bots.service import get_or_create_user
from obot.db.models import Bot, BotUser, User


async def join_bot(
    session: AsyncSession, bot: Bot, tg_user_id: int, language: str | None
) -> tuple[User, bool]:
    """Record that a person uses the bot; True when they just started (or came back)."""
    user = await get_or_create_user(session, tg_user_id, language)
    row = await session.scalar(
        select(BotUser).where(BotUser.bot_id == bot.id, BotUser.user_id == user.id)
    )
    if row is None:
        session.add(BotUser(bot_id=bot.id, user_id=user.id))
        await session.flush()
        return user, True
    if row.left_at is None:
        return user, False
    row.started_at, row.left_at = datetime.now(UTC), None
    await session.flush()
    return user, True


async def leave_bot(session: AsyncSession, bot: Bot, tg_user_id: int) -> None:
    row = await session.scalar(
        select(BotUser)
        .join(User, User.id == BotUser.user_id)
        .where(BotUser.bot_id == bot.id, User.tg_user_id == tg_user_id, BotUser.left_at.is_(None))
    )
    if row is not None:
        row.left_at = datetime.now(UTC)
        await session.flush()


async def has_started(session: AsyncSession, bot: Bot, tg_user_id: int) -> bool:
    row = await session.scalar(
        select(BotUser.id)
        .join(User, User.id == BotUser.user_id)
        .where(BotUser.bot_id == bot.id, User.tg_user_id == tg_user_id, BotUser.left_at.is_(None))
    )
    return row is not None
