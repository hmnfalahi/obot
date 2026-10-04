"""Turning a person's request into a draft task on a bot (SPEC 8.7)."""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic_ai.models import Model
from sqlalchemy.ext.asyncio import AsyncSession

from obot.billing.plans import Pricing
from obot.billing.usage import record_llm_usage
from obot.bots.users import user_bots
from obot.db.models import Bot, Task, UsageKind, User
from obot.tasks.planner import KnownBot, KnownChat, plan_tasks
from obot.tasks.service import Rejected, build_tasks, cancel_drafts, count_live_tasks

log = logging.getLogger(__name__)


class PlanningFailed(Exception):
    """The planner did not return exactly one task for the request."""


@dataclass
class TaskSync:
    scheduled: list[Task] = field(default_factory=list)
    rejected: list[Rejected] = field(default_factory=list)


async def draft_task(
    session: AsyncSession,
    pricing: Pricing,
    model: Model,
    bot: Bot,
    user: User,
    request: str,
    chats: list[KnownChat],
) -> TaskSync:
    """Plan `request` as `user`'s task on `bot`; the draft replaces earlier unconfirmed ones.

    `chats`: the groups and channels of the bot where the user is an admin.
    """
    now = datetime.now(UTC)
    peers = [
        KnownBot(b.id, b.username, (b.persona or {}).get("description", ""))
        for b in await user_bots(session, user, exclude=bot)
    ]
    result = await plan_tasks(model, [request], user.timezone, chats, now, peers)
    # Planning is setup work: billed like memory, not counted as a message.
    await record_llm_usage(
        session,
        pricing,
        user,
        bot,
        result.model_name,
        result.input_tokens,
        result.output_tokens,
        kind=UsageKind.MEMORY,
    )
    if len(result.plan.tasks) != 1:
        log.warning("planner returned %d tasks for one request", len(result.plan.tasks))
        raise PlanningFailed()
    tasks, rejected = build_tasks(
        result.plan.tasks,
        bot,
        user,
        chats,
        pricing.plan(user.plan),
        await count_live_tasks(session, user),
        now,
        peers=peers,
    )
    await cancel_drafts(session, user, bot)
    session.add_all(tasks)
    await session.flush()
    return TaskSync(tasks, rejected)
