from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic_ai.models.test import TestModel

from obot.billing.plans import ModelPrice, Pricing
from obot.bots.service import ManagedBotInfo, default_timezone, register_managed_bot
from obot.chat.membership import upsert_membership
from obot.db.models import BotStatus, Task, TaskStatus
from obot.tasks.apply import sync_tasks
from obot.tasks.planner import KnownChat, PlannedTask
from obot.tasks.schedule import InvalidSchedule, Schedule, next_run, validate
from obot.tasks.service import (
    build_tasks,
    claim_due,
    live_tasks,
    record_run,
    replace_bot_tasks,
    resume_waiting,
)
from obot.telegram.task_runner import task_prompt

PLANS_FILE = Path(__file__).parent.parent / "plans.yaml"
TEHRAN = ZoneInfo("Asia/Tehran")
NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)  # 15:30 in Tehran
OWNER = 1001


@pytest.fixture
def pricing() -> Pricing:
    pricing = Pricing.load(PLANS_FILE)
    pricing.models["test"] = ModelPrice(input_per_mtok=1, output_per_mtok=1)
    return pricing


async def make_bot(session, cipher, started: bool = True):
    async def fetch_token(_: int) -> str:
        return "9:token"

    result = await register_managed_bot(
        session, cipher, fetch_token, OWNER, ManagedBotInfo(9, "t_bot", "T"), 10
    )
    result.bot.owner_started = started
    await session.flush()
    return result.bot


def planned(**overrides) -> PlannedTask:
    values = {
        "instruction": "Send the dollar price from tgju.org.",
        "schedule": "cron",
        "cron": "0 8 * * *",
        "timezone": "Asia/Tehran",
        "destination": "owner",
        "summary": "every day at 8",
    }
    return PlannedTask(**(values | overrides))


# --- schedule arithmetic ---------------------------------------------------------------


def test_next_run_respects_timezone() -> None:
    schedule = Schedule(cron="0 8 * * *", run_at=None, timezone="Asia/Tehran")

    first = next_run(schedule, NOW)

    assert first.astimezone(TEHRAN) == datetime(2026, 10, 4, 8, 0, tzinfo=TEHRAN)
    assert first.tzinfo == UTC


def test_once_runs_then_ends() -> None:
    at = NOW + timedelta(hours=2)
    schedule = Schedule(cron=None, run_at=at, timezone="UTC")

    assert next_run(schedule, NOW) == at
    assert next_run(schedule, at) is None


@pytest.mark.parametrize(
    ("schedule", "message"),
    [
        (Schedule("*/5 * * * *", None, "UTC"), "more often"),
        (Schedule("0 8 * * *", None, "Mars/Base"), "unknown timezone"),
        (Schedule("not cron", None, "UTC"), "invalid cron"),
        (Schedule(None, NOW - timedelta(minutes=1), "UTC"), "past"),
        (Schedule(None, None, "UTC"), "no time"),
    ],
)
def test_validate_rejects(schedule, message) -> None:
    with pytest.raises(InvalidSchedule, match=message):
        validate(schedule, NOW, min_interval=timedelta(hours=1))


def test_validate_accepts_hourly_on_free_plan() -> None:
    first = validate(Schedule("0 * * * *", None, "UTC"), NOW, timedelta(hours=1))

    assert first == datetime(2026, 10, 3, 13, 0, tzinfo=UTC)


def test_persian_speakers_default_to_tehran() -> None:
    assert default_timezone("fa") == "Asia/Tehran"
    assert default_timezone("en") == "UTC"
    assert default_timezone(None) == "UTC"


# --- building tasks ----------------------------------------------------------------------


async def test_build_tasks_destinations_and_waiting_start(session, cipher, pricing) -> None:
    bot = await make_bot(session, cipher, started=False)
    chats = [KnownChat(-100, "Group", "supergroup")]
    plan = pricing.plan("free")

    tasks, rejected = build_tasks(
        [planned(), planned(destination="-100"), planned(destination="-555")],
        bot,
        chats,
        plan,
        already_live=0,
        now=NOW,
    )

    assert [t.destination_chat_id for t in tasks] == [OWNER, -100]
    assert tasks[0].status == TaskStatus.WAITING_START
    assert tasks[1].status == TaskStatus.ACTIVE
    assert [r.reason for r in rejected] == ["the bot is not a member of that chat"]


async def test_build_tasks_enforces_plan_limit(session, cipher, pricing) -> None:
    bot = await make_bot(session, cipher)

    tasks, rejected = build_tasks(
        [planned(), planned()], bot, [], pricing.plan("free"), already_live=2, now=NOW
    )

    assert len(tasks) == 1
    assert "allows 3 scheduled tasks" in rejected[0].reason


async def test_build_once_task_uses_local_time(session, cipher, pricing) -> None:
    bot = await make_bot(session, cipher)

    [task], _ = build_tasks(
        [planned(schedule="once", cron=None, run_at="2026-10-03T18:00")],
        bot,
        [],
        pricing.plan("free"),
        0,
        NOW,
    )

    assert task.next_run_at == datetime(2026, 10, 3, 18, 0, tzinfo=TEHRAN)


# --- persistence and execution bookkeeping -------------------------------------------------


async def add_task(session, bot, **overrides) -> Task:
    [task], _ = build_tasks(
        [planned(**overrides)], bot, [], Pricing.load(PLANS_FILE).plan("free"), 0, NOW
    )
    await replace_bot_tasks(session, bot, [task])
    return task


async def test_replacing_cancels_previous_tasks(session, cipher) -> None:
    bot = await make_bot(session, cipher)
    old = await add_task(session, bot)

    new = await add_task(session, bot, cron="0 9 * * *")

    assert old.status == TaskStatus.CANCELLED and old.next_run_at is None
    assert [t.id for t in await live_tasks(session, bot)] == [new.id]


async def test_claim_due_advances_before_running(session, cipher) -> None:
    bot = await make_bot(session, cipher)
    task = await add_task(session, bot)
    due_at = task.next_run_at

    assert await claim_due(session, due_at - timedelta(seconds=1), 10) == []
    claimed = await claim_due(session, due_at, 10)

    assert claimed == [task]
    assert task.last_run_at == due_at
    assert task.next_run_at == due_at + timedelta(days=1)


async def test_claimed_once_task_is_done(session, cipher) -> None:
    bot = await make_bot(session, cipher)
    task = await add_task(session, bot, schedule="once", cron=None, run_at="2026-10-03T18:00")

    await claim_due(session, task.next_run_at, 10)

    assert task.status == TaskStatus.DONE and task.next_run_at is None


async def test_tasks_of_disabled_bots_are_not_claimed(session, cipher) -> None:
    bot = await make_bot(session, cipher)
    task = await add_task(session, bot)
    bot.status = BotStatus.DISABLED
    await session.flush()

    assert await claim_due(session, task.next_run_at, 10) == []


async def test_resume_waiting_after_start(session, cipher) -> None:
    bot = await make_bot(session, cipher, started=False)
    task = await add_task(session, bot)
    assert task.status == TaskStatus.WAITING_START
    later = task.next_run_at + timedelta(hours=1)

    assert await resume_waiting(session, bot, later) == 1

    assert task.status == TaskStatus.ACTIVE
    assert task.next_run_at > later


async def test_record_run(session, cipher) -> None:
    bot = await make_bot(session, cipher)
    task = await add_task(session, bot)

    ok = await record_run(session, task, NOW)
    failed = await record_run(session, task, NOW, error="boom")

    assert ok.ok and not failed.ok and failed.error == "boom"


# --- planner + sync -------------------------------------------------------------------------


async def test_sync_tasks_plans_validates_and_replaces(session, cipher, pricing) -> None:
    bot = await make_bot(session, cipher)
    await upsert_membership(
        session, bot.id, -100, chat_type="supergroup", title="G", is_member=True, is_admin=True
    )
    model = TestModel(
        custom_output_args={
            "tasks": [
                planned().model_dump(),
                planned(cron="*/10 * * * *", summary="every 10 min").model_dump(),
            ]
        }
    )

    sync = await sync_tasks(session, pricing, model, bot, ["dollar at 8", "spam"])

    assert [t.summary for t in sync.scheduled] == ["every day at 8"]
    assert [r.summary for r in sync.rejected] == ["every 10 min"]
    assert len(await live_tasks(session, bot)) == 1


async def test_sync_with_no_requests_clears_tasks(session, cipher, pricing) -> None:
    bot = await make_bot(session, cipher)
    await add_task(session, bot)

    await sync_tasks(session, pricing, TestModel(), bot, [])

    assert await live_tasks(session, bot) == []


def test_task_prompt_asks_for_bot_language() -> None:
    task = Task(summary="daily", instruction="Send the oil price.")

    assert "'fa'" in task_prompt(task, "fa")
    assert "language" not in task_prompt(task, None)
