"""Turning a draft's task sentences into scheduled tasks of a bot."""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic_ai.models import Model
from sqlalchemy.ext.asyncio import AsyncSession

from obot.billing.plans import Pricing
from obot.billing.usage import record_llm_usage
from obot.chat.membership import list_memberships
from obot.db.models import Bot, Task, UsageKind
from obot.tasks.planner import KnownChat, plan_tasks
from obot.tasks.service import Rejected, build_tasks, count_live_tasks, replace_bot_tasks

log = logging.getLogger(__name__)


class PlanningFailed(Exception):
    """The planner did not account for every request; existing tasks are kept."""


@dataclass
class TaskSync:
    scheduled: list[Task] = field(default_factory=list)
    rejected: list[Rejected] = field(default_factory=list)


async def sync_tasks(
    session: AsyncSession,
    pricing: Pricing,
    model: Model,
    bot: Bot,
    requests: list[str],
    public_url: str | None = None,
) -> TaskSync:
    """Replace the bot's scheduled tasks with the ones described in `requests`."""
    if not requests:
        await replace_bot_tasks(session, bot, [])
        return TaskSync()
    already_live = await count_live_tasks(session, bot.owner, exclude_bot=bot)
    sync = await _plan(session, pricing, model, bot, requests, already_live, public_url)
    await replace_bot_tasks(session, bot, sync.scheduled)
    log.info(
        "tasks of bot %s replaced: %d scheduled, %d rejected",
        bot.id,
        len(sync.scheduled),
        len(sync.rejected),
    )
    return sync


async def add_tasks(
    session: AsyncSession,
    pricing: Pricing,
    model: Model,
    bot: Bot,
    requests: list[str],
    public_url: str | None = None,
) -> TaskSync:
    """Add tasks to the bot's existing ones (mini app)."""
    already_live = await count_live_tasks(session, bot.owner)
    sync = await _plan(session, pricing, model, bot, requests, already_live, public_url)
    session.add_all(sync.scheduled)
    await session.flush()
    return sync


async def _plan(
    session: AsyncSession,
    pricing: Pricing,
    model: Model,
    bot: Bot,
    requests: list[str],
    already_live: int,
    public_url: str | None,
) -> TaskSync:
    now = datetime.now(UTC)
    owner = bot.owner
    chats = [
        KnownChat(m.tg_chat_id, m.title, m.chat_type)
        for m in await list_memberships(session, bot.id)
    ]
    result = await plan_tasks(model, requests, owner.timezone, chats, now)
    # Planning is setup work: billed like memory, not counted as a message.
    await record_llm_usage(
        session,
        pricing,
        owner,
        bot,
        result.model_name,
        result.input_tokens,
        result.output_tokens,
        kind=UsageKind.MEMORY,
    )
    # One task per request: anything else means the model lost or invented some, and
    # replacing the bot's tasks with that would silently drop the owner's tasks.
    if len(result.plan.tasks) != len(requests):
        log.warning(
            "planner returned %d tasks for %d requests of bot %s",
            len(result.plan.tasks),
            len(requests),
            bot.id,
        )
        raise PlanningFailed()
    tasks, rejected = build_tasks(
        result.plan.tasks,
        bot,
        chats,
        pricing.plan(owner.plan),
        already_live,
        now,
        public_url=public_url,
    )
    return TaskSync(tasks, rejected)
