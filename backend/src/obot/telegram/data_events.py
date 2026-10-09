"""Sends the record notifications and reminders queued by obot.data.events (SPEC 8.8).

Templated messages through the send layer: no model call and no cost. People who never
started the bot, left it, blocked it or are disabled are skipped; other failures are
retried with backoff. Events of suspended or disabled bots and users are skipped (marked,
not kept for later).
"""

import logging
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from obot.bots.access import can_use
from obot.data.describe import label_of, record_lines
from obot.data.events import claim_due_reminders, mark_done, mark_failed, pending_events
from obot.data.repository import collection_schema
from obot.db.models import Bot, BotStatus, BotUser, Collection, CollectionScope, User
from obot.i18n import lang_of, t
from obot.telegram.runtime import Runtime

log = logging.getLogger(__name__)

MAX_PER_RUN = 50
GONE = ("chat not found", "user not found", "bot was blocked")


async def deliver_data_events(runtime: Runtime, now: datetime | None = None) -> int:
    """One delivery run: claim due reminders, then send what is pending. Returns how many
    messages were sent."""
    now = now or datetime.now(UTC)
    async with runtime.sessionmaker() as session:
        await claim_due_reminders(session, now)
        await session.commit()
    sent = 0
    for _ in range(MAX_PER_RUN):
        # One event per transaction: what was sent stays marked even if a later one fails.
        async with runtime.sessionmaker() as session:
            events = await pending_events(session, now, limit=1)
            if not events:
                break
            ev = events[0]
            try:
                outcome = await _deliver(session, runtime, ev)
            except Exception:
                log.exception("delivering record event %s failed", ev.id)
                outcome = None
            if outcome is None:
                mark_failed(ev, now)
            else:
                mark_done(ev, outcome, now)
                sent += outcome == "sent"
            await session.commit()
    return sent


async def _deliver(session, runtime: Runtime, ev) -> str | None:
    """'sent', 'skipped', or None to retry later."""
    bot = await session.get(Bot, ev.bot_id, options=[joinedload(Bot.owner)])
    user = await session.get(User, ev.recipient_user_id)
    if bot is None or user is None or bot.status != BotStatus.ACTIVE or user.is_disabled:
        return "skipped"
    if not can_use(bot, user.tg_user_id):
        return "skipped"
    started = await session.scalar(
        select(BotUser.id).where(
            BotUser.bot_id == bot.id, BotUser.user_id == user.id, BotUser.left_at.is_(None)
        )
    )
    if started is None:
        return "skipped"
    client = runtime.transport.client(bot.tg_bot_id)
    if client is None:
        return None  # the bot is not running right now
    c = await session.get(Collection, ev.collection_id)
    lang = lang_of(user.language)
    if c.scope == CollectionScope.PERSONAL:
        tz = ZoneInfo(user.timezone)
    else:
        owner = await session.get(User, bot.owner_id)
        tz = ZoneInfo(owner.timezone)
    text = t(
        lang,
        f"data.event.{ev.kind}",
        title=label_of(c.title, lang, c.key),
        record=record_lines(collection_schema(c), ev.data, lang, tz),
    )
    try:
        await runtime.sender.send_text(client, user.tg_user_id, text, group=False)
    except TelegramForbiddenError:
        return "skipped"  # they blocked the bot
    except TelegramBadRequest as e:
        if any(m in e.message.lower() for m in GONE):
            return "skipped"
        log.warning("sending record event %s failed", ev.id, exc_info=True)
        return None
    except Exception:
        log.warning("sending record event %s failed", ev.id, exc_info=True)
        return None
    return "sent"
