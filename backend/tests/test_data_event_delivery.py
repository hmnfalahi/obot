from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from obot.bots.users import leave_bot
from obot.data.repository import add_record, create_collection, set_collection_events
from obot.data.schema import CollectionEvents, CollectionSchema
from obot.db.models import BotStatus, BotVisibility, CollectionScope, RecordEvent
from obot.telegram.data_events import deliver_data_events

BOOKINGS = CollectionSchema.model_validate(
    {
        "fields": [
            {"key": "name", "label": {"fa": "نام", "en": "Name"}, "type": "text"},
            {"key": "date", "label": {"fa": "تاریخ", "en": "Date"}, "type": "date"},
            {"key": "time", "label": {"fa": "ساعت", "en": "Time"}, "type": "time"},
        ]
    }
)
EVENTS = CollectionEvents.model_validate(
    {
        "notify": [{"on": "create", "to": "owner"}],
        "remind": [{"date_field": "date", "time_field": "time", "before_minutes": 60}],
    }
)


async def setup(w):
    w.bot.visibility = BotVisibility.PUBLIC
    c = await create_collection(
        w.session,
        w.pricing,
        bot=w.bot,
        key="bookings",
        title={"fa": "نوبت‌ها"},
        scope=CollectionScope.SHARED,
        schema=BOOKINGS,
    )
    await set_collection_events(w.session, c, EVENTS)
    # The owner uses their bot too (started it in private).
    from obot.bots.users import join_bot

    await join_bot(w.session, w.bot, 1001, "fa")
    await add_record(
        w.session,
        w.pricing,
        await w.space(w.alice),
        c,
        {"name": "Alice", "date": "2030-01-10", "time": "17:00"},
    )
    await w.session.commit()
    sender = AsyncMock()
    runtime = SimpleNamespace(
        sessionmaker=async_sessionmaker(w.session.bind, expire_on_commit=False),
        sender=sender,
        transport=SimpleNamespace(client=MagicMock(return_value=AsyncMock())),
    )
    return c, runtime


async def outcomes(w) -> list[tuple[str, str | None]]:
    w.session.expunge_all()
    rows = await w.session.scalars(select(RecordEvent).order_by(RecordEvent.id))
    return [(e.kind, e.outcome) for e in rows]


async def test_owner_gets_a_new_booking_message_once(data_world) -> None:
    w = data_world
    _, runtime = await setup(w)

    assert await deliver_data_events(runtime) == 1
    assert await deliver_data_events(runtime) == 0

    call = runtime.sender.send_text.await_args
    assert call.args[1] == 1001
    assert "نوبت‌ها" in call.args[2] and "Alice" in call.args[2] and "۱۷:۰۰" not in call.args[2]
    assert await outcomes(w) == [("create", "sent")]


async def test_reminder_is_sent_when_due(data_world) -> None:
    w = data_world
    _, runtime = await setup(w)
    await deliver_data_events(runtime)  # the owner's message

    due = datetime(2030, 1, 10, 13, 0, tzinfo=UTC)  # 16:30 Tehran, 30 min before
    assert await deliver_data_events(runtime, now=due) == 1

    call = runtime.sender.send_text.await_args
    assert call.args[1] == 2002 and "⏰" in call.args[2]
    assert await outcomes(w) == [("create", "sent"), ("remind", "sent")]


async def test_people_who_left_are_skipped_not_retried(data_world) -> None:
    w = data_world
    _, runtime = await setup(w)
    await leave_bot(w.session, w.bot, 1001)
    await w.session.commit()

    assert await deliver_data_events(runtime) == 0
    runtime.sender.send_text.assert_not_awaited()
    assert await outcomes(w) == [("create", "skipped")]


async def test_blocked_bot_is_skipped_and_errors_are_retried(data_world) -> None:
    w = data_world
    _, runtime = await setup(w)
    runtime.sender.send_text.side_effect = TelegramForbiddenError(
        method=SendMessage(chat_id=1, text="x"), message="blocked"
    )
    await deliver_data_events(runtime)
    assert await outcomes(w) == [("create", "skipped")]


async def test_transient_failures_back_off(data_world) -> None:
    w = data_world
    _, runtime = await setup(w)
    runtime.sender.send_text.side_effect = RuntimeError("network")
    now = datetime.now(UTC)

    await deliver_data_events(runtime, now=now)
    await deliver_data_events(runtime, now=now)  # still backing off: not tried again

    assert runtime.sender.send_text.await_count == 1
    assert await outcomes(w) == [("create", None)]
    runtime.sender.send_text.side_effect = None
    assert await deliver_data_events(runtime, now=now + timedelta(minutes=5)) == 1


async def test_disabled_bots_send_nothing(data_world) -> None:
    w = data_world
    _, runtime = await setup(w)
    w.bot.status = BotStatus.DISABLED
    await w.session.commit()

    assert await deliver_data_events(runtime) == 0
    assert await outcomes(w) == [("create", "skipped")]


async def test_retention_job_purges_old_sent_events(data_world) -> None:
    from obot.data.jobs import purge_records

    w = data_world
    await setup(w)
    ev = (await w.session.scalars(select(RecordEvent))).one()
    ev.sent_at, ev.outcome = datetime.now(UTC) - timedelta(days=90), "sent"
    await w.session.commit()

    await purge_records(async_sessionmaker(w.session.bind, expire_on_commit=False), 30)

    assert await outcomes(w) == []
