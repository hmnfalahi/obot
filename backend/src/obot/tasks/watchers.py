"""Watcher triggers: poll a feed or page and fire when something new happens.

Each poll compares with what the task remembers in `trigger_state`, so the
first poll of an RSS or page watcher only records a baseline. Price watchers
fire when the number crosses to the watched side of the threshold, not on every
poll while it stays there.
"""

import hashlib
import logging
from datetime import UTC, datetime
from typing import Any

import feedparser
from pydantic import BaseModel
from pydantic_ai import Agent
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from obot.billing.usage import check_llm_quota, record_llm_usage
from obot.core.fetch import FetchError, fetch
from obot.core.tools import page_text
from obot.db.models import Bot, Task, TriggerKind, UsageKind
from obot.tasks.events import enqueue_event
from obot.tasks.service import record_run

log = logging.getLogger(__name__)

SEEN_ITEMS_KEPT = 200
MAX_NEW_ITEMS_PER_POLL = 5
EXCERPT_CHARS = 1500


async def poll_watchers(runtime: Any, task_ids: list[int]) -> None:
    """Poll each watcher once. `runtime` provides sessionmaker, http, models and pricing."""
    for task_id in task_ids:
        try:
            await poll_one(runtime, task_id)
        except Exception:
            log.exception("watcher task %s crashed", task_id)


async def poll_one(runtime: Any, task_id: int) -> None:
    now = datetime.now(UTC)
    async with runtime.sessionmaker() as session:
        task = await session.get(Task, task_id)
        bot = await session.get(Bot, task.bot_id, options=[joinedload(Bot.owner)])
        state = dict(task.trigger_state or {})
        try:
            payloads = await check(runtime, session, task, bot, state)
        except FetchError as e:
            await record_run(session, task, now, error=f"could not read the source: {e}")
            payloads = []
        # Reassign so SQLAlchemy sees the JSONB change.
        task.trigger_state = state
        for payload in payloads:
            await enqueue_event(session, task, payload)
        await session.commit()
    if payloads:
        log.info("watcher task %s fired %d event(s)", task_id, len(payloads))


async def check(
    runtime: Any, session: AsyncSession, task: Task, bot: Bot, state: dict[str, Any]
) -> list[str]:
    config = task.trigger_config
    if task.trigger == TriggerKind.RSS:
        return await check_feed(runtime.http, config["url"], state)
    if task.trigger == TriggerKind.PAGE_CHANGE:
        return await check_page(runtime.http, config["url"], state)
    if task.trigger == TriggerKind.PRICE:
        return await check_price(runtime, session, bot, config, state)
    return []


def _entry_id(entry: Any) -> str:
    return entry.get("id") or entry.get("link") or entry.get("title", "")


async def check_feed(http: Any, url: str, state: dict[str, Any]) -> list[str]:
    result = await fetch(http, url)
    feed = feedparser.parse(result.body)
    if not feed.entries:
        raise FetchError("it is not a valid RSS/Atom feed or has no items")
    ids = [_entry_id(e) for e in feed.entries]
    first_poll = "seen" not in state
    seen = set(state.get("seen", []))
    state["seen"] = list(dict.fromkeys(ids + state.get("seen", [])))[:SEEN_ITEMS_KEPT]
    if first_poll:
        return []
    new = [e for e in feed.entries if _entry_id(e) not in seen][:MAX_NEW_ITEMS_PER_POLL]
    return [
        f"New item in {feed.feed.get('title', url)}:\n{e.get('title', '')}\n{e.get('link', '')}\n"
        f"{(e.get('summary') or '')[:800]}"
        for e in reversed(new)  # oldest first
    ]


async def check_page(http: Any, url: str, state: dict[str, Any]) -> list[str]:
    text = page_text(await fetch(http, url))
    digest = hashlib.sha256(" ".join(text.split()).encode()).hexdigest()
    previous_digest = state.get("digest")
    previous_excerpt = state.get("excerpt", "")
    state["digest"] = digest
    state["excerpt"] = text[:EXCERPT_CHARS]
    if previous_digest is None or previous_digest == digest:
        return []
    return [
        f"The page {url} changed.\n\nBefore (excerpt):\n{previous_excerpt}\n\n"
        f"Now (excerpt):\n{text[:EXCERPT_CHARS]}"
    ]


class Extracted(BaseModel):
    value: float | None


async def extract_number(model: Any, what: str, text: str) -> tuple[float | None, Any]:
    agent = Agent(
        model,
        output_type=Extracted,
        instructions=(
            "Extract one number from a web page. Return null if it is not on the page. "
            "Return it in the unit named in the request, converting if the page uses "
            "another one (for Iranian prices, 1 toman = 10 rials). Return the plain number "
            "without thousands separators or units."
        ),
    )
    result = await agent.run(f"Number to extract: {what}\n\nPage text:\n{text[:6000]}")
    return result.output.value, result


async def check_price(
    runtime: Any, session: AsyncSession, bot: Bot, config: dict[str, Any], state: dict[str, Any]
) -> list[str]:
    if not (
        await check_llm_quota(session, runtime.pricing, bot.owner, bot, datetime.now(UTC))
    ).allowed:
        return []
    text = page_text(await fetch(runtime.http, config["url"]))
    model = runtime.models.for_tier("cheap")
    value, result = await extract_number(model, config["what"], text)
    await record_llm_usage(
        session,
        runtime.pricing,
        bot.owner,
        bot,
        model.model_name,
        result.usage.input_tokens,
        result.usage.output_tokens,
        kind=UsageKind.MEMORY,
    )
    if value is None:
        raise FetchError(f"could not find {config['what']} on the page")
    threshold = float(config["threshold"])
    side = "above" if value > threshold else "below"
    previous_side = state.get("side")
    state["side"] = side
    state["value"] = value
    if side != config["direction"] or previous_side == side:
        return []
    return [f"{config['what']} is now {value:,} ({side} {threshold:,}) on {config['url']}."]
