"""Schedule arithmetic: next run times and plan validation. No I/O."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cronsim import CronSim, CronSimError

# Runs sampled to find the shortest interval of a cron expression.
INTERVAL_SAMPLES = 50


class InvalidSchedule(ValueError):
    """The schedule cannot be used; the message is shown to the user."""


@dataclass(frozen=True)
class Schedule:
    cron: str | None
    run_at: datetime | None  # timezone-aware
    timezone: str


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise InvalidSchedule(f"unknown timezone '{name}'") from e


def next_run(schedule: Schedule, after: datetime) -> datetime | None:
    """First run strictly after `after`, in UTC; None when a one-off task is over."""
    if schedule.run_at is not None:
        return schedule.run_at.astimezone(UTC) if schedule.run_at > after else None
    local = after.astimezone(zone(schedule.timezone))
    try:
        return next(iter(CronSim(schedule.cron, local))).astimezone(UTC)
    except CronSimError as e:
        raise InvalidSchedule(f"invalid cron expression '{schedule.cron}': {e}") from e


def shortest_interval(cron: str, timezone: str, start: datetime) -> timedelta:
    it = CronSim(cron, start.astimezone(zone(timezone)))
    times = [next(it) for _ in range(INTERVAL_SAMPLES)]
    return min(b - a for a, b in zip(times, times[1:], strict=False))


def validate(schedule: Schedule, now: datetime, min_interval: timedelta) -> datetime:
    """Check a schedule against the plan; return its first run time in UTC."""
    zone(schedule.timezone)
    if schedule.run_at is None and not schedule.cron:
        raise InvalidSchedule("the task has no time")
    if schedule.run_at is not None:
        if schedule.run_at <= now:
            raise InvalidSchedule("the time is in the past")
        return schedule.run_at.astimezone(UTC)
    first = next_run(schedule, now)
    interval = shortest_interval(schedule.cron, schedule.timezone, now)
    if interval < min_interval:
        minutes = int(min_interval.total_seconds() // 60)
        raise InvalidSchedule(f"it runs more often than your plan allows (every {minutes} min)")
    return first
