"""People's own provider keys (bring your own key) and the model a reply uses (SPEC 8.7).

A person keeps at most one key per provider in their wallet and picks, per bot
they use, which key and model that bot answers them with. A bot's owner never
pays with their key for anyone else.
"""

from dataclasses import dataclass

from pydantic_ai.models import Model
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from obot.billing.plans import Pricing
from obot.billing.usage import allowed_tier
from obot.core.byok import Provider
from obot.core.llm import ModelRegistry
from obot.crypto import TokenCipher
from obot.db.models import Bot, ProviderKey, User, UserBotSettings


def _context(user_id: int, provider: str) -> str:
    return f"provider-key:{user_id}:{provider}"


async def wallet(session: AsyncSession, user: User) -> list[ProviderKey]:
    rows = await session.scalars(
        select(ProviderKey).where(ProviderKey.user_id == user.id).order_by(ProviderKey.provider)
    )
    return list(rows)


async def wallet_key(session: AsyncSession, user: User, provider: str) -> ProviderKey | None:
    return await session.scalar(
        select(ProviderKey).where(ProviderKey.user_id == user.id, ProviderKey.provider == provider)
    )


async def save_key(
    session: AsyncSession, cipher: TokenCipher, user: User, provider: Provider, api_key: str
) -> ProviderKey:
    """Store the key, replacing the user's previous key of that provider."""
    row = await wallet_key(session, user, provider.value)
    if row is None:
        row = ProviderKey(user_id=user.id, provider=provider.value)
        session.add(row)
    row.key_encrypted = cipher.encrypt(api_key, _context(user.id, provider.value))
    await session.flush()
    return row


async def delete_key(session: AsyncSession, user: User, provider: Provider) -> None:
    """Bots set to use this key fall back to Obot's models."""
    await session.execute(
        delete(ProviderKey).where(
            ProviderKey.user_id == user.id, ProviderKey.provider == provider.value
        )
    )


def read_key(cipher: TokenCipher, row: ProviderKey) -> str:
    return cipher.decrypt(row.key_encrypted, _context(row.user_id, row.provider))


def key_hint(cipher: TokenCipher, row: ProviderKey) -> str:
    """The key's last characters, so its owner can tell which key is stored."""
    return "…" + read_key(cipher, row)[-4:]


@dataclass(frozen=True)
class ReplyModel:
    model: Model
    # Paid at the person's own provider: free here, not counted as a message.
    byok: bool
    # The bot recommends a tier the person's plan does not include.
    downgraded: bool = False


async def reply_model(
    session: AsyncSession,
    models: ModelRegistry,
    cipher: TokenCipher,
    pricing: Pricing,
    bot: Bot,
    payer: User,
) -> ReplyModel:
    """The model `bot` answers `payer` with: their own key and model for this bot if they
    set one, else the bot's recommended tier if their plan allows it, else their best one."""
    settings = await session.scalar(
        select(UserBotSettings).where(
            UserBotSettings.user_id == payer.id, UserBotSettings.bot_id == bot.id
        )
    )
    if settings is not None and settings.key_provider and settings.model:
        key = await wallet_key(session, payer, settings.key_provider)
        if key is not None:
            model = models.byok.model(Provider(key.provider), settings.model, read_key(cipher, key))
            return ReplyModel(model, byok=True)
    tier = allowed_tier(pricing, payer, bot.model_tier)
    return ReplyModel(models.for_tier(tier), byok=False, downgraded=tier != bot.model_tier)
