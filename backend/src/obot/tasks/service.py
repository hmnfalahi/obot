from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from obot.billing.plans import Plan
from obot.db.models import (
    Bot,
    BotStatus,
    ScheduleKind,
    Task,
    TaskRun,
    TaskStatus,
    User,
)
from obot.tasks.planner import KnownBot, KnownChat, PlannedStep, PlannedTask
from obot.tasks.schedule import InvalidSchedule, Schedule, next_run, validate, zone

# Tasks that count against the plan and show in lists; drafts await confirmation.
LIVE_STATUSES = (TaskStatus.ACTIVE, TaskStatus.PAUSED)


@dataclass(frozen=True)
class Rejected:
    summary: str
    reason: str


def schedule_of(task: Task) -> Schedule:
    return Schedule(cron=task.cron, run_at=task.run_at, timezone=task.timezone)


def advance(task: Task, now: datetime) -> None:
    """Set the task's next run after `now`; a finished one-off becomes DONE."""
    task.next_run_at = next_run(schedule_of(task), now)
    if task.next_run_at is None:
        task.status = TaskStatus.DONE


def _parse_run_at(value: str | None, timezone: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as e:
        raise InvalidSchedule(f"invalid time '{value}'") from e
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=zone(timezone))


def build_tasks(
    planned: list[PlannedTask],
    bot: Bot,
    user: User,
    chats: list[KnownChat],
    plan: Plan,
    already_live: int,
    now: datetime,
    *,
    peers: Sequence[KnownBot] = (),
) -> tuple[list[Task], list[Rejected]]:
    """Draft `user`'s planned tasks on `bot`, validated against their plan, the chats
    they may post to and their bots (no I/O). Drafts run once the user confirms them."""
    accepted: list[Task] = []
    rejected: list[Rejected] = []
    chat_ids = {c.id for c in chats}
    for item in planned:
        try:
            if already_live + len(accepted) >= plan.max_scheduled_tasks:
                raise InvalidSchedule(
                    f"your plan allows {plan.max_scheduled_tasks} scheduled tasks"
                )
            task = Task(
                bot_id=bot.id,
                user_id=user.id,
                instruction=item.instruction,
                summary=item.summary,
                timezone=item.timezone,
                destination_chat_id=_destination(item.destination, user, chat_ids),
                status=TaskStatus.DRAFT,
            )
            _schedule(task, item, plan, now)
            task.steps = _chain(item.steps, bot, peers, plan)
        except InvalidSchedule as e:
            rejected.append(Rejected(item.summary, str(e)))
            continue
        accepted.append(task)
    return accepted, rejected


def _chain(steps: list[PlannedStep], bot: Bot, peers: Sequence[KnownBot], plan: Plan) -> list[dict]:
    """A chain's steps with bots resolved; [] for a single-step task."""
    if len(steps) < 2:
        return []
    if len(steps) > plan.max_chain_steps:
        raise InvalidSchedule(f"a chain can have at most {plan.max_chain_steps} steps")
    by_name = {p.username.lower(): p.id for p in peers}
    chain = []
    for step in steps:
        name = step.bot.strip().lstrip("@").lower()
        if name in ("", "self", bot.username.lower()):
            bot_id = None
        elif name in by_name:
            bot_id = by_name[name]
        else:
            raise InvalidSchedule(f"@{name} is not one of your active bots")
        chain.append({"instruction": step.instruction, "bot_id": bot_id})
    return chain


def _destination(value: str | None, user: User, chat_ids: set[int]) -> int:
    """The user's private chat with the bot, or a chat of the bot where they are admin."""
    if value in (None, "me"):
        return user.tg_user_id
    try:
        chat_id = int(value)
    except ValueError as e:
        raise InvalidSchedule("unknown chat") from e
    if chat_id not in chat_ids:
        raise InvalidSchedule("you can only post to groups and channels where you are an admin")
    return chat_id


def _schedule(task: Task, item: PlannedTask, plan: Plan, now: datetime) -> None:
    if item.schedule not in ("cron", "once"):
        raise InvalidSchedule("the task has no time")
    schedule = Schedule(
        cron=item.cron if item.schedule == "cron" else None,
        run_at=_parse_run_at(item.run_at, item.timezone) if item.schedule == "once" else None,
        timezone=item.timezone,
    )
    task.next_run_at = validate(schedule, now, timedelta(minutes=plan.min_task_interval_minutes))
    task.schedule_kind = ScheduleKind(item.schedule)
    task.cron, task.run_at = schedule.cron, schedule.run_at


async def count_live_tasks(session: AsyncSession, user: User) -> int:
    """The user's tasks on all bots, as limited by their plan."""
    return await session.scalar(
        select(func.count()).where(Task.user_id == user.id, Task.status.in_(LIVE_STATUSES))
    )


async def user_tasks(session: AsyncSession, user: User, bot: Bot | None = None) -> list[Task]:
    """The user's live tasks, on one bot or on all of them."""
    query = select(Task).where(Task.user_id == user.id, Task.status.in_(LIVE_STATUSES))
    if bot is not None:
        query = query.where(Task.bot_id == bot.id)
    return list(await session.scalars(query.order_by(Task.id)))


async def cancel_drafts(session: AsyncSession, user: User, bot: Bot) -> None:
    """Drop unconfirmed drafts: a new request replaces them."""
    await session.execute(
        update(Task)
        .where(Task.user_id == user.id, Task.bot_id == bot.id, Task.status == TaskStatus.DRAFT)
        .values(status=TaskStatus.CANCELLED, next_run_at=None)
    )


async def confirm_draft(
    session: AsyncSession, task: Task, user: User, plan: Plan, now: datetime
) -> str | None:
    """Start a confirmed draft; the reason when it can no longer start."""
    if task.status != TaskStatus.DRAFT:
        return "this task is no longer a draft"
    if await count_live_tasks(session, user) >= plan.max_scheduled_tasks:
        return f"your plan allows {plan.max_scheduled_tasks} scheduled tasks"
    if next_run(schedule_of(task), now) is None:
        task.status, task.next_run_at = TaskStatus.CANCELLED, None
        await session.flush()
        return "its time has already passed; ask me again with a new time"
    task.status = TaskStatus.ACTIVE
    if task.next_run_at is None or task.next_run_at <= now:
        advance(task, now)
    await session.flush()
    return None


async def claim_due(session: AsyncSession, now: datetime, limit: int) -> list[Task]:
    """Lock due tasks and move them to their next run before executing.

    Advancing first means a crash during execution skips a run instead of
    retrying it every minute; SKIP LOCKED lets several workers share the load.
    """
    owner, user = aliased(User), aliased(User)
    tasks = list(
        await session.scalars(
            select(Task)
            .join(Bot, Bot.id == Task.bot_id)
            .join(owner, owner.id == Bot.owner_id)
            .join(user, user.id == Task.user_id)
            .where(
                Task.status == TaskStatus.ACTIVE,
                Task.next_run_at <= now,
                Bot.status == BotStatus.ACTIVE,
                owner.is_disabled.is_(False),
                user.is_disabled.is_(False),
            )
            .order_by(Task.next_run_at)
            .limit(limit)
            .with_for_update(of=Task, skip_locked=True)
        )
    )
    for task in tasks:
        task.last_run_at = now
        advance(task, now)
    await session.flush()
    return tasks


async def record_run(
    session: AsyncSession, task: Task, started_at: datetime, error: str | None = None
) -> TaskRun:
    run = TaskRun(
        task_id=task.id,
        ok=error is None,
        error=error,
        started_at=started_at,
        # Not the server default: now() in Postgres is the transaction's start time.
        finished_at=datetime.now(UTC),
    )
    session.add(run)
    await session.flush()
    return run


async def set_paused(session: AsyncSession, task: Task, paused: bool, now: datetime) -> None:
    if paused:
        task.status = TaskStatus.PAUSED
    else:
        task.status = TaskStatus.ACTIVE
        advance(task, now)
    await session.flush()


async def cancel_task(session: AsyncSession, task: Task) -> None:
    task.status = TaskStatus.CANCELLED
    task.next_run_at = None
    await session.flush()
