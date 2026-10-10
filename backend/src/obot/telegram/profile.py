import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError

from obot.factory.designer import BotDraft

log = logging.getLogger(__name__)

SHORT_DESCRIPTION_LIMIT = 120
NETWORK_ATTEMPTS = 3


async def sync_profile(
    token: str, draft: BotDraft, *, set_name: bool, set_description: bool = True
) -> None:
    """Show the draft on the bot's Telegram profile. Failures are logged, not raised.

    Only send what changed: Telegram rate-limits setMyName for hours.
    """
    if not (set_name or set_description):
        return
    bot = Bot(token)
    try:
        for attempt in range(NETWORK_ATTEMPTS):
            try:
                if set_description:
                    await bot.set_my_description(description=draft.description)
                    await bot.set_my_short_description(
                        short_description=draft.description[:SHORT_DESCRIPTION_LIMIT]
                    )
                if set_name:
                    await bot.set_my_name(name=draft.name)
                break
            except TelegramNetworkError:
                if attempt == NETWORK_ATTEMPTS - 1:
                    raise
                await asyncio.sleep(2**attempt)
    except TelegramAPIError:
        log.warning("could not update profile of bot %s", bot.id, exc_info=True)
    finally:
        await bot.session.close()
