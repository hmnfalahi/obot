from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from obot.data.errors import InvalidSchema
from obot.data.events import (
    MAX_ATTEMPTS,
    claim_due_reminders,
    mark_done,
    mark_failed,
    pending_events,
)
from obot.data.repository import (
    add_record,
    create_collection,
    delete_record,
    set_collection_events,
    update_record,
)
from obot.data.schema import CollectionEvents, CollectionSchema
from obot.db.models import CollectionScope, RecordEvent, RecordReminder

BOOKINGS = CollectionSchema.model_validate(
    {
        "fields": [
            {"key": "name", "label": {"en": "Name"}, "type": "text", "required": True},
            {"key": "date", "label": {"en": "Date"}, "type": "date", "public": True},
            {"key": "time", "label": {"en": "Time"}, "type": "time", "public": True},
        ]
    }
)
EVENTS = CollectionEvents.model_validate(
    {
        "notify": [
            {"on": "create", "to": "owner"},
            {"on": "update", "to": "author"},
            {"on": "delete", "to": "author"},
        ],
        "remind": [{"date_field": "date", "time_field": "time", "before_minutes": 1440}],
    }
)
FAR = "2030-01-10"  # far enough in the future for reminders to be scheduled


async def bookings(w, events=EVENTS):
    c = await create_collection(
        w.session,
        w.pricing,
        bot=w.bot,
        key="bookings",
        title={"en": "Bookings"},
        scope=CollectionScope.SHARED,
        schema=BOOKINGS,
    )
    await set_collection_events(w.session, c, events)
    return c


async def events_of(session) -> list[tuple[str, int]]:
    rows = await session.scalars(select(RecordEvent).order_by(RecordEvent.id))
    return [(e.kind, e.recipient_user_id) for e in rows]


async def test_writes_queue_events_for_the_right_people(data_world) -> None:
    w = data_world
    c = await bookings(w)
    alice, owner = await w.space(w.alice), await w.space(w.owner)

    r = await add_record(
        w.session, w.pricing, alice, c, {"name": "Alice", "date": FAR, "time": "17:00"}
    )
    await update_record(w.session, w.pricing, alice, c, r.id, {"time": "18:00"})  # by herself
    await update_record(w.session, w.pricing, owner, c, r.id, {"time": "19:00"})  # by owner
    await delete_record(w.session, owner, c, r.id)

    assert await events_of(w.session) == [
        ("create", w.owner.id),  # the owner hears about new bookings
        ("update", w.alice.id),  # Alice hears when the owner changes hers, not her own edits
        ("delete", w.alice.id),
    ]
    first = await w.session.scalar(select(RecordEvent).order_by(RecordEvent.id))
    assert first.data["name"] == "Alice" and first.actor_user_id == w.alice.id


async def test_owner_is_never_told_about_their_own_bookings(data_world) -> None:
    w = data_world
    c = await bookings(w)
    await add_record(
        w.session,
        w.pricing,
        await w.space(w.owner),
        c,
        {"name": "Walk-in", "date": FAR, "time": "17:00"},
    )
    assert await events_of(w.session) == []


async def test_reminders_follow_the_record(data_world) -> None:
    w = data_world
    c = await bookings(w)
    alice = await w.space(w.alice)
    r = await add_record(
        w.session, w.pricing, alice, c, {"name": "Alice", "date": FAR, "time": "17:00"}
    )

    [rem] = list(await w.session.scalars(select(RecordReminder)))
    # Owner's zone is Asia/Tehran (UTC+3:30): 17:00 local on the 10th, a day earlier.
    assert rem.fire_at == datetime(2030, 1, 9, 13, 30, tzinfo=UTC)
    assert rem.recipient_user_id == w.alice.id

    await update_record(w.session, w.pricing, alice, c, r.id, {"time": "18:00"})
    [rem] = list(await w.session.scalars(select(RecordReminder)))
    assert rem.fire_at == datetime(2030, 1, 9, 14, 30, tzinfo=UTC)

    await delete_record(w.session, alice, c, r.id)
    assert list(await w.session.scalars(select(RecordReminder))) == []


async def test_past_and_dateless_records_get_no_reminder(data_world) -> None:
    w = data_world
    c = await bookings(w)
    alice = await w.space(w.alice)
    await add_record(
        w.session, w.pricing, alice, c, {"name": "Old", "date": "2020-01-01", "time": "10:00"}
    )
    await add_record(w.session, w.pricing, alice, c, {"name": "No date"})
    assert list(await w.session.scalars(select(RecordReminder))) == []


async def test_due_reminders_become_events_once(data_world) -> None:
    w = data_world
    c = await bookings(w)
    r = await add_record(
        w.session,
        w.pricing,
        await w.space(w.alice),
        c,
        {"name": "Alice", "date": FAR, "time": "17:00"},
    )
    later = datetime(2030, 1, 9, 14, 0, tzinfo=UTC)

    assert await claim_due_reminders(w.session, later) == 1
    assert await claim_due_reminders(w.session, later) == 0
    kinds = await events_of(w.session)
    assert ("remind", w.alice.id) in kinds
    remind = (
        await w.session.scalars(select(RecordEvent).where(RecordEvent.kind == "remind"))
    ).one()
    assert remind.record_id == r.id and remind.data["time"] == "17:00"


async def test_pending_events_retry_with_backoff_then_give_up(data_world) -> None:
    w = data_world
    c = await bookings(w)
    await add_record(
        w.session,
        w.pricing,
        await w.space(w.alice),
        c,
        {"name": "Alice", "date": FAR, "time": "17:00"},
    )
    now = datetime.now(UTC)
    [ev] = await pending_events(w.session, now)

    mark_failed(ev, now)
    assert await pending_events(w.session, now) == []  # backing off
    for _ in range(MAX_ATTEMPTS - 1):
        mark_failed(ev, now + timedelta(days=1))
    assert (ev.outcome, ev.sent_at is not None) == ("failed", True)
    assert await pending_events(w.session, now + timedelta(days=2)) == []
    mark_done(ev, "sent", now)  # idempotent on a finished event
    assert ev.outcome == "failed"


def test_owner_recipients_only_on_shared_collections() -> None:
    from obot.data.schema import check_events

    owner_rule = CollectionEvents.model_validate({"notify": [{"on": "create", "to": "owner"}]})
    with pytest.raises(InvalidSchema):
        check_events(owner_rule, BOOKINGS, CollectionScope.PERSONAL)
    check_events(owner_rule, BOOKINGS, CollectionScope.SHARED)
    bad_field = CollectionEvents.model_validate(
        {"remind": [{"date_field": "name", "before_minutes": 60}]}
    )
    with pytest.raises(InvalidSchema):
        check_events(bad_field, BOOKINGS, CollectionScope.SHARED)


async def test_changing_events_reschedules_existing_records(data_world) -> None:
    w = data_world
    c = await bookings(w, events=CollectionEvents())
    await add_record(
        w.session,
        w.pricing,
        await w.space(w.alice),
        c,
        {"name": "Alice", "date": FAR, "time": "17:00"},
    )
    assert list(await w.session.scalars(select(RecordReminder))) == []

    await set_collection_events(w.session, c, EVENTS)

    [rem] = list(await w.session.scalars(select(RecordReminder)))
    assert rem.recipient_user_id == w.alice.id
