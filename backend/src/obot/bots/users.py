"""People who use a bot: whoever started it in private (SPEC 8.7).

Starting a bot creates the person's Obot account (free plan) if needed; they
never have to visit the factory. Blocking the bot ends their use of it, and
starting it again resumes it.
"""

from datetime import UTC, datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from obot.bots.service import get_or_create_user
from obot.db.models import Bot, BotStatus, BotUser, BotVisibility, User, UserBotSettings


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


async def started_user(session: AsyncSession, bot: Bot, tg_user_id: int) -> User | None:
    """The person's account if they use the bot (started it and did not leave)."""
    return await session.scalar(
        select(User)
        .join(BotUser, BotUser.user_id == User.id)
        .where(BotUser.bot_id == bot.id, User.tg_user_id == tg_user_id, BotUser.left_at.is_(None))
    )


async def has_started(session: AsyncSession, bot: Bot, tg_user_id: int) -> bool:
    return await started_user(session, bot, tg_user_id) is not None


async def bot_settings(session: AsyncSession, user: User, bot: Bot) -> UserBotSettings:
    """The person's own settings for the bot, created on first use."""
    row = await session.scalar(
        select(UserBotSettings).where(
            UserBotSettings.user_id == user.id, UserBotSettings.bot_id == bot.id
        )
    )
    if row is None:
        row = UserBotSettings(user_id=user.id, bot_id=bot.id)
        session.add(row)
        await session.flush()
    return row


async def user_bots(session: AsyncSession, user: User, exclude: Bot | None = None) -> list[Bot]:
    """The person's bots (SPEC 8.7): the ones they made, and the ones of others they
    started and may still use (public, or private and shared with them). Active only."""
    started = (
        select(BotUser.id)
        .where(BotUser.bot_id == Bot.id, BotUser.user_id == user.id, BotUser.left_at.is_(None))
        .exists()
    )
    may_use = or_(
        Bot.visibility == BotVisibility.PUBLIC,
        Bot.allowed_users.contains([{"id": user.tg_user_id}]),
    )
    query = (
        select(Bot)
        .join(User, User.id == Bot.owner_id)
        .where(
            Bot.status == BotStatus.ACTIVE,
            User.is_disabled.is_(False),
            or_(Bot.owner_id == user.id, and_(started, may_use)),
        )
        .order_by(Bot.id)
    )
    if exclude is not None:
        query = query.where(Bot.id != exclude.id)
    return list(await session.scalars(query))
