from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic_ai.models.test import TestModel

from obot.billing.plans import ModelPrice, Pricing
from obot.bots.service import (
    ManagedBotInfo,
    default_timezone,
    get_or_create_user,
    register_managed_bot,
)
from obot.db.models import BotStatus, Task, TaskStatus
from obot.tasks.apply import PlanningFailed, draft_task
from obot.tasks.planner import KnownChat, PlannedTask
from obot.tasks.schedule import InvalidSchedule, Schedule, next_run, validate
from obot.tasks.service import (
    build_tasks,
    claim_due,
    confirm_draft,
    record_run,
    user_tasks,
)
from obot.telegram.task_runner import task_prompt

PLANS_FILE = Path(__file__).parent.parent / "plans.yaml"
TEHRAN = ZoneInfo("Asia/Tehran")
NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)  # 15:30 in Tehran
OWNER = 1001
ALI = 5005


@pytest.fixture
def pricing() -> Pricing:
    pricing = Pricing.load(PLANS_FILE)
    pricing.models["test"] = ModelPrice(input_per_mtok=1, output_per_mtok=1)
    return pricing


async def make_bot(session, cipher):
    async def fetch_token(_: int) -> str:
        return "9:token"

    result = await register_managed_bot(
        session, cipher, fetch_token, OWNER, ManagedBotInfo(9, "t_bot", "T"), 10
    )
    await session.flush()
    return result.bot


def planned(**overrides) -> PlannedTask:
    values = {
        "instruction": "Send the dollar price from tgju.org.",
        "schedule": "cron",
        "cron": "0 8 * * *",
        "timezone": "Asia/Tehran",
        "destination": "me",
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


async def test_build_tasks_drafts_for_the_user(session, cipher, pricing) -> None:
    bot = await make_bot(session, cipher)
    ali = await get_or_create_user(session, ALI)
    chats = [KnownChat(-100, "Group", "supergroup")]  # where Ali is an admin

    tasks, rejected = build_tasks(
        [planned(), planned(destination="-100"), planned(destination="-555")],
        bot,
        ali,
        chats,
        pricing.plan("free"),
        already_live=0,
        now=NOW,
    )

    assert [t.destination_chat_id for t in tasks] == [ALI, -100]
    assert {t.user_id for t in tasks} == {ali.id}
    assert {t.status for t in tasks} == {TaskStatus.DRAFT}
    assert [r.reason for r in rejected] == [
        "you can only post to groups and channels where you are an admin"
    ]


async def test_build_tasks_enforces_plan_limit(session, cipher, pricing) -> None:
    bot = await make_bot(session, cipher)

    tasks, rejected = build_tasks(
        [planned(), planned()], bot, bot.owner, [], pricing.plan("free"), already_live=2, now=NOW
    )

    assert len(tasks) == 1
    assert "allows 3 scheduled tasks" in rejected[0].reason


async def test_build_once_task_uses_local_time(session, cipher, pricing) -> None:
    bot = await make_bot(session, cipher)

    [task], _ = build_tasks(
        [planned(schedule="once", cron=None, run_at="2026-10-03T18:00")],
        bot,
        bot.owner,
        [],
        pricing.plan("free"),
        0,
        NOW,
    )

    assert task.next_run_at == datetime(2026, 10, 3, 18, 0, tzinfo=TEHRAN)


# --- persistence and execution bookkeeping -------------------------------------------------


async def add_task(session, bot, user=None, **overrides) -> Task:
    user = user or bot.owner
    plan = Pricing.load(PLANS_FILE).plan("free")
    [task], _ = build_tasks([planned(**overrides)], bot, user, [], plan, 0, NOW)
    session.add(task)
    await session.flush()
    assert await confirm_draft(session, task, user, plan, NOW) is None
    return task


async def test_tasks_are_each_users_own(session, cipher) -> None:
    bot = await make_bot(session, cipher)
    ali = await get_or_create_user(session, ALI)
    mine = await add_task(session, bot)
    alis = await add_task(session, bot, ali)

    assert [t.id for t in await user_tasks(session, bot.owner)] == [mine.id]
    assert [t.id for t in await user_tasks(session, ali, bot)] == [alis.id]


async def test_confirming_respects_the_plan(session, cipher, pricing) -> None:
    bot = await make_bot(session, cipher)
    for hour in (7, 8, 9):
        await add_task(session, bot, cron=f"0 {hour} * * *")
    [extra], _ = build_tasks([planned()], bot, bot.owner, [], pricing.plan("free"), 0, NOW)
    session.add(extra)
    await session.flush()

    problem = await confirm_draft(session, extra, bot.owner, pricing.plan("free"), NOW)

    assert "allows 3" in problem and extra.status == TaskStatus.DRAFT


async def test_claim_due_advances_before_running(session, cipher) -> None:
    bot = await make_bot(session, cipher)
    task = await add_task(session, bot)
    due_at = task.next_run_at

    assert await claim_due(session, due_at - timedelta(seconds=1), 10) == []
    claimed = await claim_due(session, due_at, 10)

    assert claimed == [task]
    assert task.last_run_at == due_at
    assert task.next_run_at == due_at + timedelta(days=1)


async def test_drafts_never_run(session, cipher, pricing) -> None:
    bot = await make_bot(session, cipher)
    [draft], _ = build_tasks([planned()], bot, bot.owner, [], pricing.plan("free"), 0, NOW)
    session.add(draft)
    await session.flush()

    assert await claim_due(session, draft.next_run_at, 10) == []


async def test_claimed_once_task_is_done(session, cipher) -> None:
    bot = await make_bot(session, cipher)
    task = await add_task(session, bot, schedule="once", cron=None, run_at="2026-10-03T18:00")

    await claim_due(session, task.next_run_at, 10)

    assert task.status == TaskStatus.DONE and task.next_run_at is None


async def test_tasks_of_disabled_bots_or_users_are_not_claimed(session, cipher) -> None:
    bot = await make_bot(session, cipher)
    ali = await get_or_create_user(session, ALI)
    task = await add_task(session, bot, ali)
    ali.is_disabled = True
    await session.flush()
    assert await claim_due(session, task.next_run_at, 10) == []

    ali.is_disabled = False
    bot.status = BotStatus.DISABLED
    await session.flush()
    assert await claim_due(session, task.next_run_at, 10) == []


async def test_record_run(session, cipher) -> None:
    bot = await make_bot(session, cipher)
    task = await add_task(session, bot)

    ok = await record_run(session, task, NOW)
    failed = await record_run(session, task, NOW, error="boom")

    assert ok.ok and not failed.ok and failed.error == "boom"


# --- planning a request ---------------------------------------------------------------------


async def test_draft_task_plans_for_the_user_and_replaces_older_drafts(
    session, cipher, pricing
) -> None:
    bot = await make_bot(session, cipher)
    ali = await get_or_create_user(session, ALI)
    model = TestModel(custom_output_args={"tasks": [planned().model_dump()]})

    first = await draft_task(session, pricing, model, bot, ali, "dollar at 8", [])
    second = await draft_task(session, pricing, model, bot, ali, "dollar at 8, again", [])

    assert first.scheduled[0].status == TaskStatus.CANCELLED
    assert second.scheduled[0].status == TaskStatus.DRAFT
    assert second.scheduled[0].user_id == ali.id
    assert await user_tasks(session, ali) == []  # drafts are not live yet


async def test_draft_task_reports_rejections(session, cipher, pricing) -> None:
    bot = await make_bot(session, cipher)
    spam = planned(cron="*/10 * * * *", summary="every 10 min").model_dump()

    sync = await draft_task(
        session, pricing, TestModel(custom_output_args={"tasks": [spam]}), bot, bot.owner, "x", []
    )

    assert sync.scheduled == [] and [r.summary for r in sync.rejected] == ["every 10 min"]


async def test_planner_losing_the_request_fails(session, cipher, pricing) -> None:
    bot = await make_bot(session, cipher)
    empty_plan = TestModel(custom_output_args={"tasks": []})

    with pytest.raises(PlanningFailed):
        await draft_task(session, pricing, empty_plan, bot, bot.owner, "dollar at 8", [])


def test_task_prompt_asks_for_bot_language() -> None:
    task = Task(summary="daily", instruction="Send the oil price.")

    assert "'fa'" in task_prompt(task, "fa")
    assert "language" not in task_prompt(task, None)


async def test_a_one_off_draft_confirmed_too_late_is_refused(session, cipher, pricing) -> None:
    bot = await make_bot(session, cipher)
    item = planned(schedule="once", cron=None, run_at="2026-10-03T18:00")
    [draft], _ = build_tasks([item], bot, bot.owner, [], pricing.plan("free"), 0, NOW)
    session.add(draft)
    await session.flush()
    too_late = datetime(2026, 10, 3, 18, 1, tzinfo=TEHRAN)

    problem = await confirm_draft(session, draft, bot.owner, pricing.plan("free"), too_late)

    assert "already passed" in problem and draft.status == TaskStatus.CANCELLED
