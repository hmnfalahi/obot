"""Turning a draft's task sentences into scheduled tasks of a bot."""

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


@dataclass
class TaskSync:
    scheduled: list[Task] = field(default_factory=list)
    rejected: list[Rejected] = field(default_factory=list)


async def sync_tasks(
    session: AsyncSession, pricing: Pricing, model: Model, bot: Bot, requests: list[str]
) -> TaskSync:
    """Replace the bot's scheduled tasks with the ones described in `requests`."""
    if not requests:
        await replace_bot_tasks(session, bot, [])
        return TaskSync()
    already_live = await count_live_tasks(session, bot.owner, exclude_bot=bot)
    sync = await _plan(session, pricing, model, bot, requests, already_live)
    await replace_bot_tasks(session, bot, sync.scheduled)
    return sync


async def add_tasks(
    session: AsyncSession, pricing: Pricing, model: Model, bot: Bot, requests: list[str]
) -> TaskSync:
    """Add tasks to the bot's existing ones (mini app)."""
    already_live = await count_live_tasks(session, bot.owner)
    sync = await _plan(session, pricing, model, bot, requests, already_live)
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
    tasks, rejected = build_tasks(
        result.plan.tasks, bot, chats, pricing.plan(owner.plan), already_live, now
    )
    return TaskSync(tasks, rejected)
