"""Sends the record notifications and reminders queued by obot.data.events (SPEC 8.8).

Templated messages through the send layer: no model call and no cost. People who never
started the bot, left it, blocked it or are disabled are skipped; other failures are
retried with backoff. Events of suspended or disabled bots and users are skipped (marked,
not kept for later).
"""

import logging
import time
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from obot.bots.access import can_use
from obot.chat.membership import get_membership
from obot.config import get_settings
from obot.data.describe import label_of, record_lines
from obot.data.events import (
    claim_due_reminders,
    event_chat,
    mark_done,
    mark_failed,
    pending_events,
    postpone,
)
from obot.data.repository import collection_schema
from obot.db.models import (
    Bot,
    BotStatus,
    BotUser,
    Collection,
    CollectionScope,
    RecordEvent,
    User,
)
from obot.i18n import lang_of, t
from obot.telegram.runtime import Runtime

log = logging.getLogger(__name__)

GONE = ("chat not found", "user not found", "bot was blocked")
LATER = "later"  # _deliver: try again soon without counting an attempt


async def deliver_data_events(
    runtime: Runtime,
    now: datetime | None = None,
    *,
    budget_seconds: float | None = None,
    claim_batch: int | None = None,
) -> int:
    """One delivery run: claim due reminders, then send what is pending until nothing is
    left or `budget_seconds` have passed (both limits default to the settings). Returns how
    many messages were sent."""
    now = now or datetime.now(UTC)
    settings = get_settings()
    budget_seconds = (
        settings.data_events_budget_seconds if budget_seconds is None else budget_seconds
    )
    claim_batch = claim_batch or settings.data_events_claim_batch
    deadline = time.monotonic() + budget_seconds
    while True:
        async with runtime.sessionmaker() as session:
            claimed = await claim_due_reminders(session, now, limit=claim_batch)
            await session.commit()
        if claimed < claim_batch or time.monotonic() >= deadline:
            break
    sent = 0
    while time.monotonic() < deadline:
        # One event per transaction: what was sent stays marked even if a later one fails.
        async with runtime.sessionmaker() as session:
            events = await pending_events(session, now, limit=1)
            if not events:
                break
            ev = events[0]
            ev_id = ev.id
            try:
                outcome = await _deliver(session, runtime, ev)
            except Exception:
                log.exception("delivering record event %s failed", ev_id)
                # Whatever the failure left in the session is dropped; the event itself is
                # still marked failed, so it cannot block every later run.
                await session.rollback()
                ev = await session.get(
                    RecordEvent, ev_id, with_for_update=True, populate_existing=True
                )
                if ev is not None and ev.sent_at is None:
                    mark_failed(ev, now)
                await session.commit()
                continue
            if outcome is None:
                mark_failed(ev, now)
            elif outcome == LATER:
                postpone(ev, now)
            else:
                mark_done(ev, outcome, now)
                sent += outcome == "sent"
            await session.commit()
    return sent


async def _deliver(session, runtime: Runtime, ev) -> str | None:
    """'sent', 'skipped', LATER to wait for the bot, or None to retry with backoff."""
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
        return LATER  # the bot is not running right now
    c = await session.get(Collection, ev.collection_id)
    lang = lang_of(user.language)
    # Group records use the owner's zone, like shared ones.
    tz = ZoneInfo(user.timezone if c.scope == CollectionScope.PERSONAL else bot.owner.timezone)
    text = t(
        lang,
        f"data.event.{ev.kind}",
        title=label_of(c.title, lang, c.key),
        record=record_lines(collection_schema(c), ev.data, lang, tz),
    )
    if c.scope == CollectionScope.GROUP:
        chat_id = await event_chat(session, ev)
        membership = await get_membership(session, bot.id, chat_id) if chat_id else None
        if membership is not None and membership.title:
            text += "\n" + t(lang, "data.event.in_group", group=membership.title)
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
