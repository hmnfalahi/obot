from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import CallbackQuery, Chat, Message, User
from pydantic_ai.models.test import TestModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import joinedload

from obot.billing.plans import ModelPrice, Pricing
from obot.bots.users import join_bot, leave_bot
from obot.db.models import Bot as BotRecord
from obot.db.models import BotVisibility, Task, TaskRun, TaskStatus
from obot.telegram import scheduling
from obot.telegram.scheduling import Scheduler
from obot.telegram.task_runner import execute_task
from tests.test_history import make_bot

PLANS_FILE = Path(__file__).parent.parent / "plans.yaml"
ALI = User(id=5005, is_bot=False, first_name="Ali", language_code="en")
PLANNED = {
    "instruction": "Send the gold price.",
    "schedule": "cron",
    "cron": "0 20 * * *",
    "timezone": "Asia/Tehran",
    "destination": "me",
    "summary": "every night at 20:00",
}


@pytest.fixture
def runtime(session):
    pricing = Pricing.load(PLANS_FILE)
    pricing.models["test"] = ModelPrice(input_per_mtok=1, output_per_mtok=1)
    model = TestModel(custom_output_args={"tasks": [PLANNED]})
    return SimpleNamespace(
        sessionmaker=async_sessionmaker(session.bind, expire_on_commit=False),
        pricing=pricing,
        models=SimpleNamespace(for_tier=lambda tier: model),
        sender=AsyncMock(),
        redis=AsyncMock(),
    )


async def public_bot(session, cipher) -> BotRecord:
    record = await session.get(
        BotRecord, await make_bot(session, cipher, 1), options=[joinedload(BotRecord.owner)]
    )
    record.visibility = BotVisibility.PUBLIC
    return record


async def test_a_user_drafts_a_task_and_confirms_it(session, cipher, runtime) -> None:
    bot = await public_bot(session, cipher)
    ali, _ = await join_bot(session, bot, ALI.id, "en")
    await session.commit()

    outcome = await Scheduler(runtime, bot, ali, AsyncMock(), "en")("gold price every night at 8")

    assert "Confirm" in outcome
    call = runtime.sender.send_text.await_args
    assert call.args[1] == ALI.id and "every night at 20:00" in call.args[2]
    confirm = call.kwargs["buttons"].inline_keyboard[0][0].callback_data
    task = await session.scalar(select(Task))
    assert (task.user_id, task.status) == (ali.id, TaskStatus.DRAFT)

    await press(session, bot, runtime, confirm, ALI)

    await session.refresh(task)
    assert task.status == TaskStatus.ACTIVE


async def press(session, bot, runtime, data: str, who: User) -> AsyncMock:
    client = AsyncMock()
    message = Message(
        message_id=3, date=datetime.now(UTC), chat=Chat(id=who.id, type="private"), text="card"
    ).as_(client)
    query = CallbackQuery(id="q", from_user=who, chat_instance="c", data=data, message=message).as_(
        client
    )
    await scheduling.on_task_button(query, session, bot, runtime)
    return client


async def test_nobody_else_can_touch_a_task(session, cipher, runtime) -> None:
    bot = await public_bot(session, cipher)
    ali, _ = await join_bot(session, bot, ALI.id, "en")
    await session.commit()
    await Scheduler(runtime, bot, ali, AsyncMock(), "en")("gold every night")
    task = await session.scalar(select(Task))
    owner = User(id=bot.owner.tg_user_id, is_bot=False, first_name="Owner")

    await press(session, bot, runtime, f"task:ok:{task.id}", owner)

    await session.refresh(task)
    assert task.status == TaskStatus.DRAFT


async def test_tasks_command_lists_only_the_users_tasks(session, cipher, runtime) -> None:
    bot = await public_bot(session, cipher)
    ali, _ = await join_bot(session, bot, ALI.id, "en")
    session.add(
        Task(
            bot_id=bot.id,
            user_id=ali.id,
            summary="nightly gold",
            instruction="x",
            schedule_kind="cron",
            cron="0 20 * * *",
            timezone="UTC",
            destination_chat_id=ALI.id,
            status=TaskStatus.ACTIVE,
        )
    )
    session.add(
        Task(
            bot_id=bot.id,
            user_id=bot.owner_id,
            summary="owner's own",
            instruction="x",
            schedule_kind="cron",
            cron="0 8 * * *",
            timezone="UTC",
            destination_chat_id=bot.owner.tg_user_id,
            status=TaskStatus.ACTIVE,
        )
    )
    await session.flush()
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=ALI.id, type="private"),
        from_user=ALI,
        text="/tasks",
    ).as_(AsyncMock())

    await scheduling.on_tasks(message, session, bot, runtime)

    text = runtime.sender.send_text.await_args.args[2]
    assert "nightly gold" in text and "owner's own" not in text


async def test_tasks_wait_while_the_user_may_not_use_the_bot(session, cipher, runtime) -> None:
    bot = await public_bot(session, cipher)
    ali, _ = await join_bot(session, bot, ALI.id, "en")
    task = Task(
        bot_id=bot.id,
        user_id=ali.id,
        summary="nightly",
        instruction="x",
        schedule_kind="cron",
        cron="0 20 * * *",
        timezone="UTC",
        destination_chat_id=ALI.id,
        status=TaskStatus.ACTIVE,
    )
    session.add(task)
    bot.visibility = BotVisibility.PRIVATE
    await session.commit()
    runtime.cipher = cipher

    await execute_task(runtime, task.id, datetime.now(UTC))
    bot.visibility = BotVisibility.PUBLIC
    await leave_bot(session, bot, ALI.id)
    await session.commit()
    await execute_task(runtime, task.id, datetime.now(UTC))

    errors = list(await session.scalars(select(TaskRun.error).order_by(TaskRun.id)))
    assert errors == ["the bot is private now", "the user stopped using the bot"]
    runtime.sender.send_text.assert_not_awaited()
