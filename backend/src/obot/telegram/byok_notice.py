import logging
from datetime import datetime

from obot.core.byok import Failure
from obot.db.models import Bot
from obot.i18n import lang_of, t
from obot.notices import first_today
from obot.telegram.runtime import Runtime

log = logging.getLogger(__name__)

PROVIDER_NAMES = {"openrouter": "OpenRouter", "openai": "OpenAI", "anthropic": "Anthropic"}


async def notify_key_failure(runtime: Runtime, bot: Bot, failure: Failure, now: datetime) -> None:
    """Tell the owner (once a day per bot and reason) that their own key failed.

    `bot.owner` must be loaded.
    """
    owner = bot.owner
    if not await first_today(runtime.redis, f"byok:{bot.id}:{failure}", now, owner.timezone):
        return
    lang = lang_of(owner.language)
    text = t(
        lang,
        "owner.byok_failed",
        username=bot.username,
        provider=PROVIDER_NAMES.get(bot.byok_provider, bot.byok_provider),
        reason=t(lang, f"byok.reason.{failure}"),
    )
    try:
        await runtime.factory_bot.send_message(owner.tg_user_id, text)
    except Exception:
        log.warning("could not notify owner %s about their key", owner.id, exc_info=True)
