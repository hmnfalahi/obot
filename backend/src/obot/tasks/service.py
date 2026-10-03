from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from obot.billing.plans import Plan
from obot.db.models import Bot, BotStatus, ScheduleKind, Task, TaskRun, TaskStatus, User
from obot.tasks.planner import KnownChat, PlannedTask
from obot.tasks.schedule import InvalidSchedule, Schedule, next_run, validate, zone

LIVE_STATUSES = (TaskStatus.ACTIVE, TaskStatus.WAITING_START, TaskStatus.PAUSED)
RUNNABLE_STATUSES = (TaskStatus.ACTIVE, TaskStatus.WAITING_START)


@dataclass(frozen=True)
class Rejected:
    summary: str
    reason: str


def schedule_of(task: Task) -> Schedule:
    return Schedule(cron=task.cron, run_at=task.run_at, timezone=task.timezone)


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
    chats: list[KnownChat],
    plan: Plan,
    already_live: int,
    now: datetime,
) -> tuple[list[Task], list[Rejected]]:
    """Validate planned tasks against the plan and the bot's chats (no I/O)."""
    accepted: list[Task] = []
    rejected: list[Rejected] = []
    chat_ids = {c.id for c in chats}
    min_interval = timedelta(minutes=plan.min_task_interval_minutes)
    for item in planned:
        try:
            if already_live + len(accepted) >= plan.max_scheduled_tasks:
                raise InvalidSchedule(
                    f"your plan allows {plan.max_scheduled_tasks} scheduled tasks"
                )
            if item.destination == "owner":
                destination = bot.owner.tg_user_id
            else:
                try:
                    destination = int(item.destination)
                except ValueError as e:
                    raise InvalidSchedule("unknown destination") from e
                if destination not in chat_ids:
                    raise InvalidSchedule("the bot is not a member of that chat")
            schedule = Schedule(
                cron=item.cron if item.schedule == "cron" else None,
                run_at=_parse_run_at(item.run_at, item.timezone)
                if item.schedule == "once"
                else None,
                timezone=item.timezone,
            )
            first = validate(schedule, now, min_interval)
        except InvalidSchedule as e:
            rejected.append(Rejected(item.summary, str(e)))
            continue
        to_owner = destination == bot.owner.tg_user_id
        accepted.append(
            Task(
                bot_id=bot.id,
                instruction=item.instruction,
                summary=item.summary,
                schedule_kind=ScheduleKind(item.schedule),
                cron=schedule.cron,
                run_at=schedule.run_at,
                timezone=schedule.timezone,
                destination_chat_id=destination,
                status=TaskStatus.WAITING_START
                if to_owner and not bot.owner_started
                else TaskStatus.ACTIVE,
                next_run_at=first,
            )
        )
    return accepted, rejected


async def count_live_tasks(session: AsyncSession, owner: User, exclude_bot: Bot) -> int:
    """Live tasks of the owner's other bots (this bot's tasks are about to be replaced)."""
    return await session.scalar(
        select(func.count())
        .select_from(Task)
        .join(Bot, Bot.id == Task.bot_id)
        .where(
            Bot.owner_id == owner.id,
            Task.bot_id != exclude_bot.id,
            Task.status.in_(LIVE_STATUSES),
        )
    )


async def replace_bot_tasks(session: AsyncSession, bot: Bot, tasks: list[Task]) -> None:
    await session.execute(
        update(Task)
        .where(Task.bot_id == bot.id, Task.status.in_(LIVE_STATUSES))
        .values(status=TaskStatus.CANCELLED, next_run_at=None)
    )
    session.add_all(tasks)
    await session.flush()


async def live_tasks(session: AsyncSession, bot: Bot) -> list[Task]:
    rows = await session.scalars(
        select(Task).where(Task.bot_id == bot.id, Task.status.in_(LIVE_STATUSES)).order_by(Task.id)
    )
    return list(rows)


async def resume_waiting(session: AsyncSession, bot: Bot, now: datetime) -> int:
    """The owner pressed Start: tasks waiting for it become active again."""
    tasks = list(
        await session.scalars(
            select(Task).where(Task.bot_id == bot.id, Task.status == TaskStatus.WAITING_START)
        )
    )
    for task in tasks:
        task.status = TaskStatus.ACTIVE
        if task.next_run_at is None or task.next_run_at <= now:
            task.next_run_at = next_run(schedule_of(task), now)
            if task.next_run_at is None:
                task.status = TaskStatus.DONE
    await session.flush()
    return len(tasks)


async def claim_due(session: AsyncSession, now: datetime, limit: int) -> list[Task]:
    """Lock due tasks and move them to their next run before executing.

    Advancing first means a crash during execution skips a run instead of
    retrying it every minute; SKIP LOCKED lets several workers share the load.
    """
    tasks = list(
        await session.scalars(
            select(Task)
            .join(Bot, Bot.id == Task.bot_id)
            .join(User, User.id == Bot.owner_id)
            .where(
                Task.status.in_(RUNNABLE_STATUSES),
                Task.next_run_at <= now,
                Bot.status == BotStatus.ACTIVE,
                User.is_disabled.is_(False),
            )
            .order_by(Task.next_run_at)
            .limit(limit)
            .with_for_update(of=Task, skip_locked=True)
        )
    )
    for task in tasks:
        task.last_run_at = now
        task.next_run_at = next_run(schedule_of(task), now)
        if task.next_run_at is None:
            task.status = TaskStatus.DONE
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
