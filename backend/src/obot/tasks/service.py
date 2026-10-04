import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from obot.billing.plans import Plan
from obot.core.fetch import normalize_url
from obot.db.models import (
    WATCHER_TRIGGERS,
    Bot,
    BotStatus,
    ScheduleKind,
    Task,
    TaskRun,
    TaskStatus,
    TriggerKind,
    User,
)
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


def advance(task: Task, now: datetime) -> None:
    """Set the task's next run after `now`, by trigger kind.

    Schedules follow their cron (a finished one-off becomes DONE), watchers poll
    every few minutes, and push triggers have no time at all.
    """
    if task.trigger == TriggerKind.SCHEDULE:
        task.next_run_at = next_run(schedule_of(task), now)
        if task.next_run_at is None:
            task.status = TaskStatus.DONE
    elif task.trigger in WATCHER_TRIGGERS:
        task.next_run_at = now + timedelta(minutes=task.trigger_config["every_minutes"])
    else:
        task.next_run_at = None


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
    *,
    public_url: str | None = None,
) -> tuple[list[Task], list[Rejected]]:
    """Validate planned tasks against the plan and the bot's chats (no I/O)."""
    accepted: list[Task] = []
    rejected: list[Rejected] = []
    chat_ids = {c.id for c in chats}
    for item in planned:
        try:
            if already_live + len(accepted) >= plan.max_scheduled_tasks:
                raise InvalidSchedule(
                    f"your plan allows {plan.max_scheduled_tasks} scheduled tasks"
                )
            destination = _chat_or_owner(item.destination, bot, chat_ids)
            task = Task(
                bot_id=bot.id,
                instruction=item.instruction,
                summary=item.summary,
                trigger=TriggerKind(item.trigger),
                timezone=item.timezone,
                destination_chat_id=destination,
            )
            _configure_trigger(task, item, chat_ids, plan, now, public_url)
        except InvalidSchedule as e:
            rejected.append(Rejected(item.summary, str(e)))
            continue
        to_owner = destination == bot.owner.tg_user_id
        task.status = (
            TaskStatus.WAITING_START if to_owner and not bot.owner_started else TaskStatus.ACTIVE
        )
        accepted.append(task)
    return accepted, rejected


def _chat_or_owner(value: str | None, bot: Bot, chat_ids: set[int]) -> int:
    if value in (None, "owner"):
        return bot.owner.tg_user_id
    try:
        chat_id = int(value)
    except ValueError as e:
        raise InvalidSchedule("unknown chat") from e
    if chat_id not in chat_ids:
        raise InvalidSchedule("the bot is not a member of that chat")
    return chat_id


def _group(value: str | None, chat_ids: set[int]) -> int:
    """A group trigger must name one of the bot's groups (never the owner's chat)."""
    try:
        chat_id = int(value or "")
    except ValueError as e:
        raise InvalidSchedule("the task needs one of the bot's groups") from e
    if chat_id not in chat_ids:
        raise InvalidSchedule("the bot is not a member of that group")
    return chat_id


def _configure_trigger(
    task: Task,
    item: PlannedTask,
    chat_ids: set[int],
    plan: Plan,
    now: datetime,
    public_url: str | None,
) -> None:
    kind = task.trigger
    if kind == TriggerKind.SCHEDULE:
        if item.schedule not in ("cron", "once"):
            raise InvalidSchedule("the task has no time")
        schedule = Schedule(
            cron=item.cron if item.schedule == "cron" else None,
            run_at=_parse_run_at(item.run_at, item.timezone) if item.schedule == "once" else None,
            timezone=item.timezone,
        )
        task.next_run_at = validate(
            schedule, now, timedelta(minutes=plan.min_task_interval_minutes)
        )
        task.schedule_kind = ScheduleKind(item.schedule)
        task.cron, task.run_at = schedule.cron, schedule.run_at
        return

    config: dict = {}
    if kind in WATCHER_TRIGGERS:
        if not item.url:
            raise InvalidSchedule("the task needs a web address to watch")
        # Checking more often than the plan allows is raised to the minimum.
        config["url"] = normalize_url(item.url)
        config["every_minutes"] = max(item.every_minutes or 60, plan.min_task_interval_minutes)
        task.next_run_at = now  # first poll records the baseline
    if kind == TriggerKind.PRICE:
        if not item.what or item.threshold is None or item.direction not in ("above", "below"):
            raise InvalidSchedule("a price alert needs what to watch, a threshold and a direction")
        config.update(what=item.what, threshold=item.threshold, direction=item.direction)
    if kind in (TriggerKind.GROUP_KEYWORD, TriggerKind.NEW_MEMBER):
        config["chat_id"] = _group(item.source_chat, chat_ids)
    if kind == TriggerKind.GROUP_KEYWORD:
        keywords = [k.strip() for k in item.keywords if k.strip()]
        if not keywords:
            raise InvalidSchedule("the task needs keywords to look for")
        config["keywords"] = keywords
    if kind == TriggerKind.WEBHOOK:
        if not public_url:
            raise InvalidSchedule("this server has no public address for webhooks")
        task.hook_secret = secrets.token_urlsafe(24)
    task.trigger_config = config


def hook_url(public_url: str, task: Task) -> str:
    return f"{public_url.rstrip('/')}/api/hooks/{task.id}/{task.hook_secret}"


async def count_live_tasks(
    session: AsyncSession, owner: User, exclude_bot: Bot | None = None
) -> int:
    """Live tasks of the owner, optionally without one bot whose tasks are being replaced."""
    query = (
        select(func.count())
        .select_from(Task)
        .join(Bot, Bot.id == Task.bot_id)
        .where(Bot.owner_id == owner.id, Task.status.in_(LIVE_STATUSES))
    )
    if exclude_bot is not None:
        query = query.where(Task.bot_id != exclude_bot.id)
    return await session.scalar(query)


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
            advance(task, now)
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
