from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
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


async def test_blocked_bot_is_skipped(data_world) -> None:
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


async def extra_event(w, c, recipient, kind="create", data=None) -> None:
    w.session.add(
        RecordEvent(
            bot_id=w.bot.id,
            collection_id=c.id,
            record_id=1,
            kind=kind,
            recipient_user_id=recipient.id,
            data=data or {"name": "Bob"},
        )
    )
    await w.session.commit()


async def test_one_failing_event_does_not_stop_or_resend_the_others(
    data_world, monkeypatch
) -> None:
    from obot.telegram import data_events

    w = data_world
    c, runtime = await setup(w)
    await extra_event(w, c, w.owner)
    real = data_events.record_lines
    calls = []

    def flaky(*args):
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("broken")
        return real(*args)

    monkeypatch.setattr(data_events, "record_lines", flaky)
    now = datetime.now(UTC)

    assert await deliver_data_events(runtime, now=now) == 1
    assert await deliver_data_events(runtime, now=now) == 0
    assert runtime.sender.send_text.await_count == 1
    assert await outcomes(w) == [("create", None), ("create", "sent")]


async def test_a_crash_after_a_success_keeps_it_marked_sent(data_world, monkeypatch) -> None:
    from obot.telegram import data_events

    w = data_world
    c, runtime = await setup(w)
    await extra_event(w, c, w.owner)
    real = data_events.record_lines
    calls = []

    def second_raises(*args):
        calls.append(1)
        if len(calls) == 2:
            raise ValueError("boom")
        return real(*args)

    monkeypatch.setattr(data_events, "record_lines", second_raises)
    assert await deliver_data_events(runtime) == 1
    assert await outcomes(w) == [("create", "sent"), ("create", None)]


async def test_people_who_can_no_longer_use_a_private_bot_are_skipped(data_world) -> None:
    w = data_world
    c, runtime = await setup(w)
    w.bot.visibility = BotVisibility.PRIVATE
    await extra_event(w, c, w.alice)

    assert await deliver_data_events(runtime) == 1  # the owner's only
    assert await outcomes(w) == [("create", "sent"), ("create", "skipped")]


async def test_unreachable_chats_are_skipped(data_world) -> None:
    w = data_world
    _, runtime = await setup(w)
    runtime.sender.send_text.side_effect = TelegramBadRequest(
        method=SendMessage(chat_id=1, text="x"), message="Bad Request: chat not found"
    )
    await deliver_data_events(runtime)
    assert await outcomes(w) == [("create", "skipped")]


async def test_update_template_and_personal_timezone(data_world) -> None:
    w = data_world
    _, runtime = await setup(w)
    p = await create_collection(
        w.session,
        w.pricing,
        bot=w.bot,
        key="visits",
        title={"en": "Visits"},
        scope=CollectionScope.PERSONAL,
        schema=CollectionSchema.model_validate(
            {"fields": [{"key": "at", "label": {"en": "At"}, "type": "datetime"}]}
        ),
    )
    w.alice.timezone = "Asia/Tokyo"
    w.alice.language = "en"
    await extra_event(w, p, w.alice, kind="update", data={"at": "2030-01-10T00:00:00Z"})
    await deliver_data_events(runtime)

    texts = [c.args[2] for c in runtime.sender.send_text.await_args_list]
    assert "✏️ Changed in «Visits»:\nAt: 2030-01-10 09:00" in texts


async def test_one_run_sends_everything_pending_within_its_budget(data_world) -> None:
    w = data_world
    c, runtime = await setup(w)
    for _ in range(59):
        w.session.add(
            RecordEvent(
                bot_id=w.bot.id,
                collection_id=c.id,
                record_id=1,
                kind="create",
                recipient_user_id=w.owner.id,
                data={"name": "Bob"},
            )
        )
    await w.session.commit()

    assert await deliver_data_events(runtime, budget_seconds=0) == 0
    assert await deliver_data_events(runtime) == 60  # more than the old fixed 50


async def test_reminders_are_claimed_in_batches_until_none_are_due(data_world) -> None:
    w = data_world
    c, runtime = await setup(w)
    for name in ("Bob", "Carol"):
        await add_record(
            w.session,
            w.pricing,
            await w.space(w.alice),
            c,
            {"name": name, "date": "2030-01-10", "time": "17:00"},
        )
    await w.session.commit()
    due = datetime(2030, 1, 10, 13, 0, tzinfo=UTC)

    await deliver_data_events(runtime, now=due, claim_batch=1)

    kinds = [k for k, _ in await outcomes(w)]
    assert kinds.count("remind") == 3


async def test_a_bot_that_is_not_running_does_not_use_up_attempts(data_world) -> None:
    w = data_world
    _, runtime = await setup(w)
    runtime.transport.client.return_value = None
    now = datetime.now(UTC)

    assert await deliver_data_events(runtime, now=now) == 0

    w.session.expunge_all()
    ev = (await w.session.scalars(select(RecordEvent))).one()
    assert (ev.attempts, ev.sent_at) == (0, None)
    assert ev.next_attempt_at == now + timedelta(minutes=1)


async def test_an_event_that_breaks_its_transaction_does_not_block_the_rest(
    data_world, monkeypatch
) -> None:
    from sqlalchemy import text

    from obot.telegram import data_events

    w = data_world
    c, runtime = await setup(w)
    await extra_event(w, c, w.owner)
    real = data_events._deliver
    calls = []

    async def poisoned(session, runtime, ev):
        calls.append(1)
        if len(calls) == 1:
            await session.execute(text("select 1/0"))  # leaves the transaction aborted
        return await real(session, runtime, ev)

    monkeypatch.setattr(data_events, "_deliver", poisoned)

    assert await deliver_data_events(runtime) == 1
    w.session.expunge_all()
    first = await w.session.scalar(select(RecordEvent).order_by(RecordEvent.id))
    assert (first.attempts, first.sent_at) == (1, None)
    assert await outcomes(w) == [("create", None), ("create", "sent")]
