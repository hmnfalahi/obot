"""A bot's own provider key (bring your own key) and the model its replies use."""

from dataclasses import dataclass

from pydantic_ai.models import Model

from obot.billing.plans import Pricing
from obot.billing.usage import allowed_tier
from obot.core.byok import Provider
from obot.core.llm import ModelRegistry
from obot.crypto import TokenCipher
from obot.db.models import Bot


def uses_byok(bot: Bot) -> bool:
    return bot.byok_key_encrypted is not None


def _context(bot: Bot) -> str:
    return f"byok:{bot.id}"


def set_key(cipher: TokenCipher, bot: Bot, provider: Provider, model: str, api_key: str) -> None:
    bot.byok_provider = provider.value
    bot.byok_model = model
    bot.byok_key_encrypted = cipher.encrypt(api_key, _context(bot))


def clear_key(bot: Bot) -> None:
    bot.byok_provider = bot.byok_model = bot.byok_key_encrypted = None


def read_key(cipher: TokenCipher, bot: Bot) -> str:
    return cipher.decrypt(bot.byok_key_encrypted, _context(bot))


def key_hint(cipher: TokenCipher, bot: Bot) -> str:
    """The key's last characters, so the owner can tell which key is stored."""
    return "…" + read_key(cipher, bot)[-4:]


@dataclass(frozen=True)
class ReplyModel:
    model: Model
    byok: bool


def reply_model(
    models: ModelRegistry, cipher: TokenCipher, pricing: Pricing, bot: Bot
) -> ReplyModel:
    """The model for the bot's replies: its own key if set, else its plan's tier.

    `bot.owner` must be loaded.
    """
    if uses_byok(bot):
        key = read_key(cipher, bot)
        model = models.byok.model(Provider(bot.byok_provider), bot.byok_model, key)
        return ReplyModel(model, byok=True)
    tier = allowed_tier(pricing, bot.owner, bot.model_tier)
    return ReplyModel(models.for_tier(tier), byok=False)
