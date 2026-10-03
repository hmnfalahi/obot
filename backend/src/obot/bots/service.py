import enum
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy import func, select
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
    token: str | None  # plaintext; None when the update was ignored

    @property
    def should_run(self) -> bool:
        return self.token is not None and self.bot.status == BotStatus.ACTIVE


def default_timezone(language: str | None) -> str:
    """Best guess until users can set it: Persian speakers are mostly in Iran."""
    return "Asia/Tehran" if (language or "").startswith("fa") else "UTC"


async def get_or_create_user(
    session: AsyncSession, tg_user_id: int, language: str | None = None
) -> User:
    user = await session.scalar(select(User).where(User.tg_user_id == tg_user_id))
    if user is None:
        user = User(tg_user_id=tg_user_id, language=language, timezone=default_timezone(language))
        session.add(user)
        await session.flush()
    return user


async def register_managed_bot(
    session: AsyncSession,
    cipher: TokenCipher,
    fetch_token: TokenFetcher,
    creator_tg_id: int,
    info: ManagedBotInfo,
    max_bots: int,
) -> RegisterResult:
    """Handle a `managed_bot` update: creation, token replacement or owner change.

    Telegram sends the same update for all three, including after our own
    replaceManagedBotToken (kill switch), so this must be idempotent and must
    never revive a disabled bot. A bot that would exceed the owner's `max_bots`
    is stored as suspended.
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
            status=await _status_within_limit(session, owner, max_bots),
        )
        session.add(bot)
        await session.flush()
        return RegisterResult(bot, RegisterOutcome.CREATED, token)

    bot.username = info.username
    bot.display_name = info.display_name

    if bot.owner_id != owner.id:
        # Count before reassigning: autoflush would otherwise count this bot too.
        bot.status = await _status_within_limit(session, owner, max_bots)
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


async def count_active_bots(session: AsyncSession, owner: User) -> int:
    return await session.scalar(
        select(func.count()).where(Bot.owner_id == owner.id, Bot.status == BotStatus.ACTIVE)
    )


async def _status_within_limit(session: AsyncSession, owner: User, max_bots: int) -> BotStatus:
    if await count_active_bots(session, owner) >= max_bots:
        return BotStatus.SUSPENDED
    return BotStatus.ACTIVE


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
