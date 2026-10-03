"""Kill switch, bans and abuse reports (SPEC 3.8)."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from obot.bots.service import count_active_bots
from obot.crypto import TokenCipher
from obot.db.models import Bot, BotStatus, Report, ReportStatus, User

# Revokes a managed bot's token (replaceManagedBotToken) and returns the new one.
TokenReplacer = Callable[[int], Awaitable[str]]

MAX_REASON_LENGTH = 1000


async def find_bot(session: AsyncSession, username: str) -> Bot | None:
    return await session.scalar(
        select(Bot)
        .options(joinedload(Bot.owner))
        # Exact, case-insensitive: LIKE would treat the "_" in usernames as a wildcard.
        .where(func.lower(Bot.username) == username.strip().lstrip("@").lower())
    )


async def disable_bot(
    session: AsyncSession, cipher: TokenCipher, replace_token: TokenReplacer, bot: Bot
) -> None:
    """Stop serving a bot and revoke its token.

    The new token is stored, so an admin can enable the bot again. Telegram's
    follow-up managed_bot update is ignored because the bot is already disabled.
    """
    bot.status = BotStatus.DISABLED
    # Commit before revoking: Telegram's managed_bot update for the new token is
    # handled concurrently in another session and must already see "disabled",
    # otherwise it stores the token as an update and starts the bot again.
    await session.commit()
    new_token = await replace_token(bot.tg_bot_id)
    bot.token_encrypted = cipher.encrypt(new_token, str(bot.tg_bot_id))
    await session.flush()


async def enable_bot(session: AsyncSession, bot: Bot, max_bots: int) -> BotStatus:
    """Re-enable a disabled bot, as suspended if the owner's plan has no room."""
    room = await count_active_bots(session, bot.owner) < max_bots
    bot.status = BotStatus.ACTIVE if room else BotStatus.SUSPENDED
    await session.flush()
    return bot.status


async def owned_bots(session: AsyncSession, user: User) -> list[Bot]:
    rows = await session.scalars(
        select(Bot).options(joinedload(Bot.owner)).where(Bot.owner_id == user.id)
    )
    return list(rows)


async def ban_user(
    session: AsyncSession, cipher: TokenCipher, replace_token: TokenReplacer, user: User
) -> list[Bot]:
    """Disable an account and kill all of its bots; returns the bots that were killed."""
    user.is_disabled = True
    killed = []
    for bot in await owned_bots(session, user):
        if bot.status != BotStatus.DISABLED:
            await disable_bot(session, cipher, replace_token, bot)
            killed.append(bot)
    await session.flush()
    return killed


async def unban_user(session: AsyncSession, user: User) -> None:
    """Lift the account ban. Its bots stay disabled until enabled one by one."""
    user.is_disabled = False
    await session.flush()


async def create_report(
    session: AsyncSession, bot: Bot, reporter_tg_id: int, tg_chat_id: int | None, reason: str
) -> Report:
    report = Report(
        bot_id=bot.id,
        reporter_tg_id=reporter_tg_id,
        tg_chat_id=tg_chat_id,
        reason=reason.strip()[:MAX_REASON_LENGTH],
    )
    session.add(report)
    await session.flush()
    return report


async def open_reports(session: AsyncSession, limit: int = 20) -> list[Report]:
    rows = await session.scalars(
        select(Report)
        .where(Report.status == ReportStatus.OPEN)
        .order_by(Report.created_at)
        .limit(limit)
    )
    return list(rows)


async def resolve_reports(session: AsyncSession, bot: Bot, status: ReportStatus) -> int:
    """Close all open reports of a bot, e.g. after disabling it."""
    reports = list(
        await session.scalars(
            select(Report).where(Report.bot_id == bot.id, Report.status == ReportStatus.OPEN)
        )
    )
    now = datetime.now(UTC)
    for report in reports:
        report.status = status
        report.resolved_at = now
    await session.flush()
    return len(reports)
