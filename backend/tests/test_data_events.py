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
    reschedule_collection,
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


async def reminder(session) -> RecordReminder | None:
    return await session.scalar(select(RecordReminder).execution_options(populate_existing=True))


async def test_due_unclaimed_reminder_survives_an_unrelated_edit(data_world) -> None:
    from zoneinfo import ZoneInfo

    w = data_world
    c = await bookings(w)
    alice = await w.space(w.alice)
    # The booking is in two hours, so its day-before reminder fell due while the job had
    # not run yet (as if it had been scheduled earlier).
    local = (datetime.now(UTC) + timedelta(hours=2)).astimezone(ZoneInfo("Asia/Tehran"))
    day, at = local.date().isoformat(), local.strftime("%H:%M")
    r = await add_record(w.session, w.pricing, alice, c, {"name": "Alice", "date": day, "time": at})
    moment = datetime.combine(local.date(), local.time().replace(second=0, microsecond=0))
    fire_at = moment.replace(tzinfo=ZoneInfo("Asia/Tehran")) - timedelta(days=1)
    w.session.add(
        RecordReminder(
            record_id=r.id,
            collection_id=c.id,
            rule=0,
            recipient_user_id=w.alice.id,
            fire_at=fire_at,
        )
    )
    await w.session.flush()

    await update_record(w.session, w.pricing, alice, c, r.id, {"name": "Alice B"})
    await set_collection_events(w.session, c, EVENTS)

    assert await claim_due_reminders(w.session, datetime.now(UTC)) == 1


async def test_sent_reminder_is_not_resent_after_an_unrelated_edit(data_world) -> None:
    w = data_world
    c = await bookings(w)
    alice = await w.space(w.alice)
    r = await add_record(
        w.session, w.pricing, alice, c, {"name": "Alice", "date": FAR, "time": "17:00"}
    )
    assert await claim_due_reminders(w.session, datetime(2030, 1, 9, 14, 0, tzinfo=UTC)) == 1

    await update_record(w.session, w.pricing, alice, c, r.id, {"name": "Alice B"})
    await set_collection_events(w.session, c, EVENTS)

    rem = await reminder(w.session)
    assert rem.sent_at is not None
    assert await claim_due_reminders(w.session, datetime(2030, 1, 9, 14, 0, tzinfo=UTC)) == 0


async def test_sent_reminder_is_armed_again_when_the_date_moves(data_world) -> None:
    w = data_world
    c = await bookings(w)
    alice = await w.space(w.alice)
    r = await add_record(
        w.session, w.pricing, alice, c, {"name": "Alice", "date": FAR, "time": "17:00"}
    )
    assert await claim_due_reminders(w.session, datetime(2030, 1, 9, 14, 0, tzinfo=UTC)) == 1

    await update_record(w.session, w.pricing, alice, c, r.id, {"date": "2030-02-10"})

    rem = await reminder(w.session)
    assert rem.sent_at is None
    assert rem.fire_at == datetime(2030, 2, 9, 13, 30, tzinfo=UTC)


async def test_schema_change_drops_reminders_that_no_longer_fit(data_world) -> None:
    from obot.data.events import collection_events
    from obot.data.repository import update_collection_schema

    w = data_world
    c = await bookings(w)
    dateless = CollectionSchema.model_validate(
        {"fields": [{"key": "name", "label": {"en": "Name"}, "type": "text", "required": True}]}
    )
    await update_collection_schema(w.session, c, dateless)

    events = collection_events(c)
    assert events.remind == [] and len(events.notify) == 3
    await add_record(w.session, w.pricing, await w.space(w.alice), c, {"name": "Alice"})
    assert await reminder(w.session) is None


async def test_reminder_on_a_field_that_does_not_fit_is_ignored(data_world) -> None:
    w = data_world
    c = await bookings(w, events=CollectionEvents())
    # Stored by an older version or by hand: a reminder on a text field.
    c.events = {"remind": [{"date_field": "name", "before_minutes": 60}]}
    await w.session.flush()

    await add_record(
        w.session,
        w.pricing,
        await w.space(w.alice),
        c,
        {"name": "Alice", "date": FAR, "time": "17:00"},
    )
    assert await reminder(w.session) is None


async def remind_events(session) -> list[RecordEvent]:
    return list(await session.scalars(select(RecordEvent).where(RecordEvent.kind == "remind")))


async def test_a_reminder_claimed_after_the_booking_is_dropped(data_world) -> None:
    w = data_world
    c = await bookings(w)
    await add_record(
        w.session,
        w.pricing,
        await w.space(w.alice),
        c,
        {"name": "Alice", "date": FAR, "time": "17:00"},
    )
    # The job was down: the booking (13:30 UTC on the 10th) has passed before it ran.
    after = datetime(2030, 1, 10, 14, 0, tzinfo=UTC)

    assert await claim_due_reminders(w.session, after) == 1
    assert await remind_events(w.session) == []
    assert (await reminder(w.session)).sent_at == after
    assert await claim_due_reminders(w.session, after) == 0


async def test_a_reminder_at_the_time_is_still_sent_just_after_it(data_world) -> None:
    w = data_world
    at_the_time = CollectionEvents.model_validate(
        {"remind": [{"date_field": "date", "time_field": "time", "before_minutes": 0}]}
    )
    c = await bookings(w, events=at_the_time)
    await add_record(
        w.session,
        w.pricing,
        await w.space(w.alice),
        c,
        {"name": "Alice", "date": FAR, "time": "17:00"},
    )

    assert await claim_due_reminders(w.session, datetime(2030, 1, 10, 13, 31, tzinfo=UTC)) == 1
    assert len(await remind_events(w.session)) == 1


async def test_a_personal_reminder_uses_the_authors_zone_when_claimed(data_world) -> None:
    w = data_world
    c = await create_collection(
        w.session,
        w.pricing,
        bot=w.bot,
        key="visits",
        title={"en": "Visits"},
        scope=CollectionScope.PERSONAL,
        schema=BOOKINGS,
    )
    await set_collection_events(
        w.session,
        c,
        CollectionEvents.model_validate(
            {"remind": [{"date_field": "date", "time_field": "time", "before_minutes": 60}]}
        ),
    )
    w.alice.timezone = "Asia/Tokyo"  # 17:00 there is 08:00 UTC
    await add_record(
        w.session,
        w.pricing,
        await w.space(w.alice),
        c,
        {"name": "Alice", "date": FAR, "time": "17:00"},
    )
    # 10:00 UTC is before the booking in Tehran but after it in Tokyo.
    assert await claim_due_reminders(w.session, datetime(2030, 1, 10, 10, 0, tzinfo=UTC)) == 1
    assert await remind_events(w.session) == []


async def test_rescheduling_leaves_past_records_alone(data_world) -> None:
    w = data_world
    c = await bookings(w, events=CollectionEvents())
    alice = await w.space(w.alice)
    old = await add_record(
        w.session, w.pricing, alice, c, {"name": "Old", "date": "2020-01-01", "time": "10:00"}
    )
    await add_record(w.session, w.pricing, alice, c, {"name": "New", "date": FAR, "time": "17:00"})
    # A row an older rule left on the past record: rescheduling would have removed it.
    stale = datetime(2019, 12, 1, tzinfo=UTC)
    w.session.add(
        RecordReminder(
            record_id=old.id,
            collection_id=c.id,
            rule=0,
            recipient_user_id=w.alice.id,
            fire_at=stale,
        )
    )
    await w.session.flush()

    await set_collection_events(w.session, c, EVENTS)

    expected = [(True, stale), (False, datetime(2030, 1, 9, 13, 30, tzinfo=UTC))]
    assert await reminder_rows(w.session, old.id) == expected
    # Rescheduling again with the same rules changes nothing.
    await reschedule_collection(w.session, c, w.owner, datetime.now(UTC))
    assert await reminder_rows(w.session, old.id) == expected


async def reminder_rows(session, past_id: int) -> list[tuple[bool, datetime]]:
    rows = await session.scalars(
        select(RecordReminder)
        .order_by(RecordReminder.record_id)
        .execution_options(populate_existing=True)
    )
    return [(r.record_id == past_id, r.fire_at) for r in rows if r.sent_at is None]


async def test_owners_are_never_told_about_personal_records(data_world) -> None:
    w = data_world
    c = await create_collection(
        w.session,
        w.pricing,
        bot=w.bot,
        key="notes",
        title={"en": "Notes"},
        scope=CollectionScope.PERSONAL,
        schema=BOOKINGS,
    )
    # Stored by an older version or by hand; check_events refuses it.
    c.events = {"notify": [{"on": "create", "to": "owner"}]}
    await w.session.flush()

    await add_record(w.session, w.pricing, await w.space(w.alice), c, {"name": "Alice"})
    assert await events_of(w.session) == []


async def test_an_edit_that_changes_nothing_tells_nobody(data_world) -> None:
    w = data_world
    c = await bookings(w)
    r = await add_record(
        w.session,
        w.pricing,
        await w.space(w.alice),
        c,
        {"name": "Alice", "date": FAR, "time": "17:00"},
    )
    owner = await w.space(w.owner)

    await update_record(w.session, w.pricing, owner, c, r.id, {"time": "17:00"})

    assert await events_of(w.session) == [("create", w.owner.id)]
