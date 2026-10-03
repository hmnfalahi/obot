import enum
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from obot.crypto import TokenCipher
from obot.db.models import Bot, BotStatus, User

# Fetches the current token of a managed bot from Telegram (getManagedBotToken).
TokenFetcher = Callable[[int], Awaitable[str]]


class RegisterOutcome(enum.StrEnum):
    CREATED = "created"
    UNCHANGED = "unchanged"
    TOKEN_UPDATED = "token_updated"
    OWNER_CHANGED = "owner_changed"
    IGNORED_DISABLED = "ignored_disabled"


@dataclass(frozen=True)
class ManagedBotInfo:
    tg_bot_id: int
    username: str
    display_name: str


@dataclass(frozen=True)
class RegisterResult:
    bot: Bot
    outcome: RegisterOutcome
    token: str | None  # plaintext, only when the bot should (re)start


async def get_or_create_user(
    session: AsyncSession, tg_user_id: int, language: str | None = None
) -> User:
    user = await session.scalar(select(User).where(User.tg_user_id == tg_user_id))
    if user is None:
        user = User(tg_user_id=tg_user_id, language=language)
        session.add(user)
        await session.flush()
    return user


async def register_managed_bot(
    session: AsyncSession,
    cipher: TokenCipher,
    fetch_token: TokenFetcher,
    creator_tg_id: int,
    info: ManagedBotInfo,
) -> RegisterResult:
    """Handle a `managed_bot` update: creation, token replacement or owner change.

    Telegram sends the same update for all three, including after our own
    replaceManagedBotToken (kill switch), so this must be idempotent and must
    never revive a disabled bot.
    """
    bot = await session.scalar(
        select(Bot).options(joinedload(Bot.owner)).where(Bot.tg_bot_id == info.tg_bot_id)
    )

    if bot is not None and bot.status == BotStatus.DISABLED:
        return RegisterResult(bot, RegisterOutcome.IGNORED_DISABLED, token=None)

    owner = await get_or_create_user(session, creator_tg_id)
    token = await fetch_token(info.tg_bot_id)
    context = str(info.tg_bot_id)

    if bot is None:
        bot = Bot(
            owner=owner,
            tg_bot_id=info.tg_bot_id,
            username=info.username,
            display_name=info.display_name,
            token_encrypted=cipher.encrypt(token, context),
            webhook_secret=secrets.token_urlsafe(32),
        )
        session.add(bot)
        await session.flush()
        return RegisterResult(bot, RegisterOutcome.CREATED, token)

    bot.username = info.username
    bot.display_name = info.display_name

    if bot.owner_id != owner.id:
        bot.owner = owner
        bot.owner_started = False
        bot.token_encrypted = cipher.encrypt(token, context)
        outcome = RegisterOutcome.OWNER_CHANGED
    elif cipher.decrypt(bot.token_encrypted, context) != token:
        bot.token_encrypted = cipher.encrypt(token, context)
        outcome = RegisterOutcome.TOKEN_UPDATED
    else:
        outcome = RegisterOutcome.UNCHANGED

    await session.flush()
    return RegisterResult(bot, outcome, token)


async def get_bot_by_tg_id(session: AsyncSession, tg_bot_id: int) -> Bot | None:
    return await session.scalar(
        select(Bot).options(joinedload(Bot.owner)).where(Bot.tg_bot_id == tg_bot_id)
    )


async def list_active_bots(session: AsyncSession) -> list[Bot]:
    rows = await session.scalars(
        select(Bot)
        .join(Bot.owner)
        .where(Bot.status == BotStatus.ACTIVE, User.is_disabled.is_(False))
    )
    return list(rows)


async def mark_owner_started(session: AsyncSession, bot: Bot, tg_user_id: int) -> bool:
    """Record that the owner pressed Start. Returns True if this was the first time."""
    if bot.owner.tg_user_id != tg_user_id or bot.owner_started:
        return False
    bot.owner_started = True
    await session.flush()
    return True
