"""Who may use a bot: private bots serve their owner and up to ten people (SPEC 8.7).

Telegram enforces it too (setManagedBotAccessSettings); we check again so a
stale setting never lets a stranger through.
"""

import logging

from aiogram import Bot as Client
from aiogram.exceptions import TelegramAPIError
from aiogram.methods import SetManagedBotAccessSettings

from obot.db.models import Bot, BotVisibility

log = logging.getLogger(__name__)

# Telegram's limit of extra people on a restricted bot.
MAX_ALLOWED_USERS = 10


def can_use(bot: Bot, tg_user_id: int) -> bool:
    """`bot.owner` must be loaded."""
    if bot.visibility == BotVisibility.PUBLIC or tg_user_id == bot.owner.tg_user_id:
        return True
    return any(u["id"] == tg_user_id for u in bot.allowed_users or [])


async def sync_access(factory: Client, bot: Bot) -> bool:
    """Tell Telegram who may open the bot. False (logged) if Telegram refused."""
    private = bot.visibility == BotVisibility.PRIVATE
    try:
        await factory(
            SetManagedBotAccessSettings(
                user_id=bot.tg_bot_id,
                is_access_restricted=private,
                added_user_ids=[u["id"] for u in bot.allowed_users or []] if private else None,
            )
        )
    except TelegramAPIError:
        log.warning("could not set access of bot %s", bot.id, exc_info=True)
        return False
    return True
