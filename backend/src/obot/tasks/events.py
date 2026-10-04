"""Event-triggered tasks (SPEC 8.5, P2-5).

Every fired trigger becomes a TaskEvent row, whoever fires it: group handlers
(keyword, new member), the API process (webhooks) or watchers in the bot
process. A job in the bot process claims pending rows and runs their tasks, so
events cross processes through the database.
"""

import hmac
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from obot.db.models import Bot, BotStatus, Task, TaskEvent, TaskStatus, TriggerKind, User

MAX_PAYLOAD_CHARS = 4000
# Webhook calls accepted per task per hour; protects the owner's credits.
MAX_HOOK_EVENTS_PER_HOUR = 60


async def enqueue_event(session: AsyncSession, task: Task, payload: str) -> TaskEvent:
    event = TaskEvent(task_id=task.id, payload=payload[:MAX_PAYLOAD_CHARS])
    session.add(event)
    await session.flush()
    return event


async def claim_events(session: AsyncSession, limit: int) -> list[TaskEvent]:
    """Lock and mark pending events of runnable tasks; the caller runs them."""
    events = list(
        await session.scalars(
            select(TaskEvent)
            .join(Task, Task.id == TaskEvent.task_id)
            .join(Bot, Bot.id == Task.bot_id)
            .join(User, User.id == Bot.owner_id)
            .where(
                TaskEvent.processed_at.is_(None),
                Task.status.in_((TaskStatus.ACTIVE, TaskStatus.WAITING_START)),
                Bot.status == BotStatus.ACTIVE,
                User.is_disabled.is_(False),
            )
            .order_by(TaskEvent.id)
            .limit(limit)
            .with_for_update(of=TaskEvent, skip_locked=True)
        )
    )
    now = datetime.now(UTC)
    for event in events:
        event.processed_at = now
    await session.flush()
    return events


async def chat_trigger_tasks(
    session: AsyncSession, bot_id: int, chat_id: int, kind: TriggerKind
) -> list[Task]:
    """Active push-triggered tasks of a bot that watch one group."""
    rows = await session.scalars(
        select(Task).where(
            Task.bot_id == bot_id,
            Task.trigger == kind,
            Task.status == TaskStatus.ACTIVE,
            # JSONB containment: casting to INTEGER would overflow on real group ids.
            Task.trigger_config.contains({"chat_id": chat_id}),
        )
    )
    return list(rows)


def keyword_hit(task: Task, text: str) -> bool:
    lowered = text.casefold()
    return any(
        k.strip() and k.strip().casefold() in lowered
        for k in task.trigger_config.get("keywords", [])
    )


class HookRejected(Exception):
    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status


async def accept_webhook(session: AsyncSession, task_id: int, secret: str, payload: str) -> None:
    """Validate an incoming webhook call and queue it."""
    task = await session.get(Task, task_id)
    if (
        task is None
        or task.trigger != TriggerKind.WEBHOOK
        or not task.hook_secret
        or not hmac.compare_digest(task.hook_secret, secret)
    ):
        raise HookRejected(404, "unknown hook")
    if task.status != TaskStatus.ACTIVE:
        raise HookRejected(409, "this task is not active")
    recent = await session.scalar(
        select(func.count()).where(
            TaskEvent.task_id == task.id,
            TaskEvent.created_at >= datetime.now(UTC) - timedelta(hours=1),
        )
    )
    if recent >= MAX_HOOK_EVENTS_PER_HOUR:
        raise HookRejected(429, "too many calls in the last hour")
    await enqueue_event(session, task, payload)
