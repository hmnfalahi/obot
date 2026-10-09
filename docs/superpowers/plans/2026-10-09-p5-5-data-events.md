# P5-5: Notifications and Reminders for Records Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A bot's collections can tell people about records. The owner gets a message on each new booking, the person who booked is told when the owner changes or cancels it, and they are reminded a set time before it. Messages are templated (no model call, no cost), sent through the single send layer, and the factory designs and shows them ("با هر نوبت جدید خبرت می‌کنم • یک روز قبل یادآوری می‌کنم").

**Architecture:** A transactional outbox in `obot.data`, with four parts:
- **Outbox rows:** record writes (`add_record`, `update_record`, `delete_record`) insert `RecordEvent` rows in the same transaction, so events exist exactly when the write commits, whether it came from chat or the panel.
- **Reminders:** they are scheduled as `RecordReminder` rows and recomputed when a record's date changes or a collection's events change.
- **Delivery job:** an in-process job (`obot/telegram/data_events.py`, run every 15 s) does two things: it turns due reminders into outbox rows, and it sends pending rows with retries, skipping people who never started the bot or blocked it.
- **Factory:** the designer gets three event switches per collection. `sync_collections` stores them through `set_collection_events`.

**Tech Stack:** Python 3.13, SQLAlchemy 2 async + Alembic, Postgres (`FOR UPDATE SKIP LOCKED`), pydantic 2, aiogram 3, pydantic-ai.

**Spec:** `docs/SPEC.md` section ۸٫۸, P5-5 and the event decisions:
- "رکورد جدید یا تغییر ← پیام به صاحب یا نویسنده"
- "X قبل از فیلد تاریخ ← یادآوری"
- "قالب‌دار، بدون فراخوانی مدل و بدون هزینه"
- "شکست ارسال رکورد را برنمی‌گرداند"
- "گیرنده‌ای که ربات را Start نکرده فقط ثبت می‌شود"
- "یادآوری‌ها… از tasks جدا… در تیک زمان‌بند (SKIP LOCKED)"

Builds on P5-1..P5-4 on branch `p5-data-core`.

## Global Constraints

- **No model, no cost:** event messages are templates in `obot/i18n.py` (fa/en). They never call a model and are never billed.
- **Send path:** all sending goes through `runtime.sender`.
- **Outbox in the same transaction:** events are written with the record write. A failed send never undoes or blocks the write. Delivery retries up to 5 times with backoff, then gives up and records the outcome.
- **Owner recipient:** `to: owner` is only allowed on SHARED collections. The owner never hears about personal records (SPEC 3.4).
- **Recipients:** a person is never notified of their own action. A recipient who has not started the bot, has left it or blocked it, or is disabled is skipped and recorded, not retried. A disabled bot sends nothing.
- **Reminder timing:**
  - Reminders are computed in the collection's time zone: the owner's for shared/group collections, the author's for personal ones.
  - A date-only field is reminded at 09:00 local.
  - Reminders in the past are not scheduled.
  - Deleting a record cancels its reminders.
- **Factory designs, never deletes:** event switches the draft omits keep their current value (same rule as P5-4's C1 fix). Removing a reminder needs the switch to be set explicitly.
- **Separate from tasks:** reminders do not count against the plan's scheduled-task limits.
- **Commits:** one plain English imperative sentence, ending with a `Co-Authored-By:` trailer. Never commit the user's untracked `backend/avatars.jsonl` or `backend/scripts/`. Never run `ruff --fix` outside `src/` and `tests/`.
- **Tests:** dev infra must be up. Run tests from `backend/`.

## File Structure

| File | Responsibility |
|---|---|
| `backend/src/obot/db/models.py` (modify) + migration | `RecordEvent` (outbox), `RecordReminder` |
| `backend/src/obot/data/schema.py` (modify) | `NotifyRule`, `RemindRule`, `CollectionEvents`, `check_events` |
| `backend/src/obot/data/events.py` (create) | Queue events and schedule reminders on writes; claim due reminders; pending events; mark sent/failed; purge |
| `backend/src/obot/data/repository.py` (modify) | Call `events` from add/update/delete; `set_collection_events` |
| `backend/src/obot/data/describe.py` (modify) | `record_lines` (moved from `data_chat.record_text`), `events_text` |
| `backend/src/obot/telegram/data_events.py` (create) | `deliver_data_events(runtime)`: the delivery job |
| `backend/src/obot/telegram/data_chat.py` (modify) | `record_text` uses `describe.record_lines` |
| `backend/src/obot/telegram/bootstrap.py`, `backend/src/obot/data/jobs.py` (modify) | Run the job every 15 s; purge old events |
| `backend/src/obot/factory/collections.py`, `designer.py` (modify) | Event switches in drafts; sync stores them; designer instructions |
| `backend/src/obot/telegram/factory.py`, `backend/src/obot/i18n.py` (modify) | Card lines for events; message templates |
| Tests: `test_data_events.py`, `test_data_event_delivery.py` (create); `test_factory_collections.py`, `test_factory.py`, `test_data_describe.py` (modify) | |

---

### Task 1: The outbox and reminders in `obot.data`

**Files:**
- Modify: `backend/src/obot/db/models.py`; create a migration with `uv run alembic revision --autogenerate -m "record events"`
- Modify: `backend/src/obot/data/schema.py`
- Create: `backend/src/obot/data/events.py`
- Modify: `backend/src/obot/data/repository.py`
- Test: `backend/tests/test_data_events.py`

**Interfaces:**
- Produces (models):
  - `RecordEvent(id, bot_id, collection_id, record_id: int | None, kind: str, recipient_user_id, actor_user_id: int | None, data: dict, created_at, next_attempt_at, attempts: int, sent_at: datetime | None, outcome: str | None)`.
  - `RecordReminder(id, record_id, collection_id, rule: int, recipient_user_id, fire_at, sent_at)`, unique on `(record_id, rule)`.
- Produces (schema):
  - `NotifyRule(on: Literal["create","update","delete"], to: Literal["owner","author"])`.
  - `RemindRule(date_field: str, time_field: str | None = None, before_minutes: int (0..43200), to: Literal["author","owner"] = "author")`.
  - `CollectionEvents(notify: list[NotifyRule] = [], remind: list[RemindRule] = [] (max 3))`.
  - `check_events(events, schema, scope) -> None` raises `InvalidSchema`.
- Produces (events.py):
  - `collection_events(c) -> CollectionEvents`.
  - `async on_record_change(session, c, record, kind: str, actor_user_id: int, tz: ZoneInfo, now: datetime) -> None`.
  - `async reschedule_collection(session, c, now) -> None`.
  - `async claim_due_reminders(session, now, limit=100) -> int`.
  - `async pending_events(session, now, limit=50) -> list[RecordEvent]`.
  - `mark_done(ev, outcome: str, now)` and `mark_failed(ev, now)`.
  - `async purge_events(session, retention_days) -> int`.
  - `MAX_ATTEMPTS = 5`.
- Produces (repository): `async set_collection_events(session, c, events: CollectionEvents) -> None`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_data_events.py`:

```python
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
        w.session, w.pricing, bot=w.bot, key="bookings", title={"en": "Bookings"},
        scope=CollectionScope.SHARED, schema=BOOKINGS,
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
        w.session, w.pricing, await w.space(w.owner), c,
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
        w.session, w.pricing, await w.space(w.alice), c,
        {"name": "Alice", "date": FAR, "time": "17:00"},
    )
    later = datetime(2030, 1, 9, 14, 0, tzinfo=UTC)

    assert await claim_due_reminders(w.session, later) == 1
    assert await claim_due_reminders(w.session, later) == 0
    kinds = await events_of(w.session)
    assert ("remind", w.alice.id) in kinds
    remind = (await w.session.scalars(
        select(RecordEvent).where(RecordEvent.kind == "remind")
    )).one()
    assert remind.record_id == r.id and remind.data["time"] == "17:00"


async def test_pending_events_retry_with_backoff_then_give_up(data_world) -> None:
    w = data_world
    c = await bookings(w)
    await add_record(
        w.session, w.pricing, await w.space(w.alice), c,
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
    assert ev.outcome == "sent"


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
        w.session, w.pricing, await w.space(w.alice), c,
        {"name": "Alice", "date": FAR, "time": "17:00"},
    )
    assert list(await w.session.scalars(select(RecordReminder))) == []

    await set_collection_events(w.session, c, EVENTS)

    [rem] = list(await w.session.scalars(select(RecordReminder)))
    assert rem.recipient_user_id == w.alice.id
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && uv run pytest tests/test_data_events.py -q`
Expected: FAIL with `ImportError` (`CollectionEvents` / `obot.data.events` missing).

- [ ] **Step 3: Models and migration**

Append to `backend/src/obot/db/models.py` after `RecordKey`:

```python
class RecordEvent(Base):
    """A message about a record waiting to be sent: an outbox row written in the same
    transaction as the record change (SPEC 8.8). `data` is the record as it was then."""

    __tablename__ = "record_events"
    __table_args__ = (Index("ix_record_events_pending", "sent_at", "next_attempt_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id", ondelete="CASCADE"))
    collection_id: Mapped[int] = mapped_column(
        ForeignKey("collections.id", ondelete="CASCADE"), index=True
    )
    # No foreign key: a deleted record may be purged before its message is sent.
    record_id: Mapped[int | None] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(16))  # create | update | delete | remind
    recipient_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    actor_user_id: Mapped[int | None] = mapped_column(BigInteger)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    next_attempt_at: Mapped[datetime] = mapped_column(server_default=func.now())
    attempts: Mapped[int] = mapped_column(default=0, server_default="0")
    sent_at: Mapped[datetime | None]
    # sent | skipped (recipient can't be reached) | failed (gave up after retries)
    outcome: Mapped[str | None] = mapped_column(String(16))


class RecordReminder(Base):
    """When to remind someone about a record; recomputed when its date changes."""

    __tablename__ = "record_reminders"
    __table_args__ = (
        UniqueConstraint("record_id", "rule"),
        Index("ix_record_reminders_due", "sent_at", "fire_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    record_id: Mapped[int] = mapped_column(ForeignKey("records.id", ondelete="CASCADE"))
    collection_id: Mapped[int] = mapped_column(
        ForeignKey("collections.id", ondelete="CASCADE"), index=True
    )
    rule: Mapped[int]  # index into the collection's remind rules
    recipient_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    fire_at: Mapped[datetime]
    sent_at: Mapped[datetime | None]
```

Then run `uv run alembic upgrade head && uv run alembic revision --autogenerate -m "record events"`. Check that the migration only creates these two tables and their indexes, then run `uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head`.

- [ ] **Step 4: Event rules in the schema**

Append to `backend/src/obot/data/schema.py` (`InvalidSchema` import from `obot.data.errors`; `Literal` from typing; `CollectionScope` imported inside the function to avoid a models import cycle if needed):

```python
class NotifyRule(BaseModel):
    on: Literal["create", "update", "delete"]
    to: Literal["owner", "author"]


class RemindRule(BaseModel):
    # A date field plus a time field, or a datetime field, or a date field alone (09:00).
    date_field: str
    time_field: str | None = None
    before_minutes: int = Field(ge=0, le=30 * 24 * 60)
    to: Literal["author", "owner"] = "author"


class CollectionEvents(BaseModel):
    notify: list[NotifyRule] = []
    remind: list[RemindRule] = Field(default=[], max_length=3)


def check_events(events: CollectionEvents, schema: CollectionSchema, scope) -> None:
    """Refuse rules that name missing fields or tell the owner about personal records."""
    from obot.db.models import CollectionScope

    rules = [*events.notify, *events.remind]
    if scope != CollectionScope.SHARED and any(r.to == "owner" for r in rules):
        raise InvalidSchema("only shared collections can notify the owner")
    fields = schema.field_map()
    for r in events.remind:
        day = fields.get(r.date_field)
        if r.time_field is None:
            ok = day is not None and day.type in (FieldType.DATE, FieldType.DATETIME)
        else:
            at = fields.get(r.time_field)
            ok = (
                day is not None
                and day.type == FieldType.DATE
                and at is not None
                and at.type == FieldType.TIME
            )
        if not ok:
            raise InvalidSchema(
                "a reminder needs a date or datetime field, or a date and a time field"
            )
```

- [ ] **Step 5: The events module**

Create `backend/src/obot/data/events.py`:

```python
"""Notifications and reminders about records (SPEC 8.8): an outbox written in the same
transaction as the record, and reminders the delivery job turns into outbox rows.

Only obot.data writes here; obot.telegram.data_events sends the messages.
"""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from obot.data.schema import CollectionEvents, CollectionSchema, FieldType, RemindRule
from obot.db.models import Collection, Record, RecordEvent, RecordReminder, User

MAX_ATTEMPTS = 5
DATE_ONLY_AT = time(9, 0)  # reminders for a date without a time


def collection_events(c: Collection) -> CollectionEvents:
    return CollectionEvents.model_validate(c.events or {})


def _schema(c: Collection) -> CollectionSchema:
    return CollectionSchema.model_validate({"fields": c.schema["fields"], "rules": c.rules})


def moment_of(rule: RemindRule, data: dict, schema: CollectionSchema, tz: ZoneInfo):
    """The record's moment for a remind rule, or None when it has no date."""
    raw = data.get(rule.date_field)
    if raw is None:
        return None
    if schema.field(rule.date_field).type == FieldType.DATETIME:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    at = DATE_ONLY_AT
    if rule.time_field is not None:
        if data.get(rule.time_field) is None:
            return None
        at = time.fromisoformat(data[rule.time_field])
    return datetime.combine(date.fromisoformat(raw), at, tzinfo=tz).astimezone(UTC)
```

Continue the same module:

```python
def _recipient(owner_id: int, record: Record, to: str) -> int:
    return owner_id if to == "owner" else record.author_user_id


async def _schedule(
    session: AsyncSession,
    c: Collection,
    record: Record,
    owner_id: int,
    tz: ZoneInfo,
    now: datetime,
) -> None:
    await session.execute(delete(RecordReminder).where(RecordReminder.record_id == record.id))
    schema = _schema(c)
    for i, rule in enumerate(collection_events(c).remind):
        moment = moment_of(rule, record.data, schema, tz)
        if moment is None:
            continue
        fire_at = moment - timedelta(minutes=rule.before_minutes)
        if fire_at <= now:
            continue
        session.add(
            RecordReminder(
                record_id=record.id,
                collection_id=c.id,
                rule=i,
                recipient_user_id=_recipient(owner_id, record, rule.to),
                fire_at=fire_at,
            )
        )


async def on_record_change(
    session: AsyncSession,
    c: Collection,
    record: Record,
    kind: str,
    *,
    owner_id: int,
    actor_user_id: int,
    tz: ZoneInfo,
    now: datetime,
) -> None:
    """Queue notifications for a create/update/delete and keep the record's reminders in
    step. Runs in the writer's transaction, so nothing is sent unless the write commits."""
    for rule in collection_events(c).notify:
        if rule.on != kind:
            continue
        recipient = _recipient(owner_id, record, rule.to)
        if recipient == actor_user_id:
            continue  # nobody is told about their own action
        session.add(
            RecordEvent(
                bot_id=c.bot_id,
                collection_id=c.id,
                record_id=record.id,
                kind=kind,
                recipient_user_id=recipient,
                actor_user_id=actor_user_id,
                data=dict(record.data),
            )
        )
    if kind == "delete":
        await session.execute(
            delete(RecordReminder).where(RecordReminder.record_id == record.id)
        )
    else:
        await _schedule(session, c, record, owner_id, tz, now)
    await session.flush()


async def reschedule_collection(
    session: AsyncSession, c: Collection, owner: User, now: datetime
) -> None:
    """Recompute reminders of every live record after the collection's events changed."""
    records = await session.scalars(
        select(Record).where(Record.collection_id == c.id, Record.deleted_at.is_(None))
    )
    from obot.db.models import CollectionScope

    for record in records:
        if c.scope == CollectionScope.PERSONAL:
            author = await session.get(User, record.author_user_id)
            tz = ZoneInfo(author.timezone)
        else:
            tz = ZoneInfo(owner.timezone)
        await _schedule(session, c, record, owner.id, tz, now)
    await session.flush()


async def claim_due_reminders(session: AsyncSession, now: datetime, limit: int = 100) -> int:
    """Turn due reminders into outbox rows (once each); the caller commits."""
    due = list(
        await session.scalars(
            select(RecordReminder)
            .where(RecordReminder.sent_at.is_(None), RecordReminder.fire_at <= now)
            .order_by(RecordReminder.fire_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    for rem in due:
        rem.sent_at = now
        record = await session.get(Record, rem.record_id)
        if record is None or record.deleted_at is not None:
            continue
        session.add(
            RecordEvent(
                bot_id=record.bot_id,
                collection_id=rem.collection_id,
                record_id=record.id,
                kind="remind",
                recipient_user_id=rem.recipient_user_id,
                data=dict(record.data),
            )
        )
    await session.flush()
    return len(due)


async def pending_events(
    session: AsyncSession, now: datetime, limit: int = 50
) -> list[RecordEvent]:
    """Events to send now, locked for this delivery run."""
    rows = await session.scalars(
        select(RecordEvent)
        .where(RecordEvent.sent_at.is_(None), RecordEvent.next_attempt_at <= now)
        .order_by(RecordEvent.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return list(rows)


def mark_done(ev: RecordEvent, outcome: str, now: datetime) -> None:
    if ev.sent_at is None:
        ev.sent_at, ev.outcome = now, outcome


def mark_failed(ev: RecordEvent, now: datetime) -> None:
    """Back off 1, 2, 4, 8 minutes; give up after MAX_ATTEMPTS."""
    ev.attempts += 1
    if ev.attempts >= MAX_ATTEMPTS:
        mark_done(ev, "failed", now)
    else:
        ev.next_attempt_at = now + timedelta(minutes=2 ** (ev.attempts - 1))


async def purge_events(session: AsyncSession, retention_days: int) -> int:
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    result = await session.execute(
        delete(RecordEvent).where(RecordEvent.sent_at < cutoff)
    )
    return result.rowcount
```

Move the `CollectionScope` import to the module top if no import cycle arises.

`test_reminders_follow_the_record` asserts `datetime(..., tzinfo=UTC)` equality. Make sure `fire_at` comes back timezone-aware: the models map `datetime` to `DateTime(timezone=True)`.

- [ ] **Step 6: Wire the repository**

In `backend/src/obot/data/repository.py`:
- Import `on_record_change` and `reschedule_collection` from `obot.data.events`, plus `CollectionEvents` and `check_events` from schema.
- In `add_record`, after the `begin_nested` block, call `await on_record_change(session, c, record, "create", owner_id=space.owner_id, actor_user_id=space.user_id, tz=tz, now=datetime.now(UTC))`.
- In `update_record`, after its `begin_nested` block, call the same with `"update"`.
- In `delete_record`, after flagging and deleting the keys, call it with `"delete"` and `tz=tz_for(space, c)`.
- Add:

```python
async def set_collection_events(
    session: AsyncSession, c: Collection, events: CollectionEvents
) -> None:
    """Replace the collection's notifications and reminders and reschedule its records'
    reminders. Callers check that the caller may edit the collection."""
    check_events(events, collection_schema(c), c.scope)
    c.events = events.model_dump(mode="json")
    owner = await session.scalar(
        select(User).join(Bot, Bot.owner_id == User.id).where(Bot.id == c.bot_id)
    )
    await reschedule_collection(session, c, owner, datetime.now(UTC))
```

- [ ] **Step 7: Run tests, lint, commit**

Run: `cd backend && uv run pytest tests/test_data_events.py tests/test_data_*.py -q && uv run pytest -q && uv run ruff check . && uv run ruff format --check .`

```bash
git add backend/src/obot/db/models.py backend/alembic/versions backend/src/obot/data/schema.py backend/src/obot/data/events.py backend/src/obot/data/repository.py backend/tests/test_data_events.py
git commit -m "Queue record notifications and reminders with the write that causes them

Co-Authored-By: <your model> <noreply@anthropic.com>"
```

---

### Task 2: Delivering the messages

**Files:**
- Modify: `backend/src/obot/data/describe.py` (`record_lines`, moved from `data_chat`)
- Modify: `backend/src/obot/telegram/data_chat.py` (`record_text` delegates)
- Create: `backend/src/obot/telegram/data_events.py`
- Modify: `backend/src/obot/i18n.py`, `backend/src/obot/telegram/bootstrap.py`, `backend/src/obot/data/jobs.py`
- Test: `backend/tests/test_data_event_delivery.py`

**Interfaces:**
- Consumes: Task 1 `claim_due_reminders`, `pending_events`, `mark_done`, `mark_failed`, `purge_events`.
- Produces:
  - `describe.record_lines(schema, data: dict, lang, tz) -> str`.
  - `async deliver_data_events(runtime, now: datetime | None = None) -> int` (messages sent).
  - The i18n keys `data.event.create|update|delete|remind`.
  - The jobs `data-events` (15 s) and the purge extension.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_data_event_delivery.py`:

```python
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
        w.session, w.pricing, bot=w.bot, key="bookings", title={"fa": "نوبت‌ها"},
        scope=CollectionScope.SHARED, schema=BOOKINGS,
    )
    await set_collection_events(w.session, c, EVENTS)
    # The owner uses their bot too (started it in private).
    from obot.bots.users import join_bot

    await join_bot(w.session, w.bot, 1001, "fa")
    await add_record(
        w.session, w.pricing, await w.space(w.alice), c,
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
```

In `test_owner_gets_a_new_booking_message_once`, the record has no datetime field, so "17:00" is shown as stored. Drop the `"۱۷:۰۰" not in` clause if it gets in the way; it only documents that time values are not re-digitised.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && uv run pytest tests/test_data_event_delivery.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'obot.telegram.data_events'`.

- [ ] **Step 3: `record_lines` in describe**

Move the body of `data_chat.record_text` and its `_shown` helper into `backend/src/obot/data/describe.py` as `record_lines(schema: CollectionSchema, data: dict, lang: str, tz: ZoneInfo) -> str`. Keep the same output: "Label: value" lines, hidden fields left out, datetimes in `tz`. Make `data_chat.record_text(c, record, lang, tz)` return `record_lines(collection_schema(c), record.data, lang, tz)`. The existing chat tests must stay green.

- [ ] **Step 4: i18n**

`"en"`:

```python
        "data.event.create": "🆕 New in «{title}»:\n{record}",
        "data.event.update": "✏️ Changed in «{title}»:\n{record}",
        "data.event.delete": "🗑 Removed from «{title}»:\n{record}",
        "data.event.remind": "⏰ Reminder — «{title}»:\n{record}",
```

`"fa"`:

```python
        "data.event.create": "🆕 مورد تازه در «{title}»:\n{record}",
        "data.event.update": "✏️ تغییر در «{title}»:\n{record}",
        "data.event.delete": "🗑 از «{title}» حذف شد:\n{record}",
        "data.event.remind": "⏰ یادآوری — «{title}»:\n{record}",
```

- [ ] **Step 5: The delivery job**

Create `backend/src/obot/telegram/data_events.py`:

```python
"""Sends the record notifications and reminders queued by obot.data.events (SPEC 8.8).

Templated messages through the send layer: no model call and no cost. People who never
started the bot, left it, blocked it or are disabled are skipped; other failures are
retried with backoff.
"""

import logging
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramForbiddenError
from sqlalchemy import select

from obot.data.describe import label_of, record_lines
from obot.data.events import claim_due_reminders, mark_done, mark_failed, pending_events
from obot.data.repository import collection_schema
from obot.db.models import Bot, BotStatus, BotUser, Collection, CollectionScope, User
from obot.i18n import lang_of, t
from obot.telegram.runtime import Runtime

log = logging.getLogger(__name__)


async def deliver_data_events(runtime: Runtime, now: datetime | None = None) -> int:
    """One delivery run: claim due reminders, then send what is pending. Returns how many
    messages were sent."""
    now = now or datetime.now(UTC)
    async with runtime.sessionmaker() as session:
        await claim_due_reminders(session, now)
        await session.commit()
    sent = 0
    async with runtime.sessionmaker() as session:
        for ev in await pending_events(session, now):
            outcome = await _deliver(session, runtime, ev)
            if outcome is None:
                mark_failed(ev, now)
            else:
                mark_done(ev, outcome, now)
                sent += outcome == "sent"
        await session.commit()
    return sent


async def _deliver(session, runtime: Runtime, ev) -> str | None:
    """'sent', 'skipped', or None to retry later."""
    bot = await session.get(Bot, ev.bot_id)
    user = await session.get(User, ev.recipient_user_id)
    if bot is None or user is None or bot.status != BotStatus.ACTIVE or user.is_disabled:
        return "skipped"
    started = await session.scalar(
        select(BotUser.id).where(
            BotUser.bot_id == bot.id, BotUser.user_id == user.id, BotUser.left_at.is_(None)
        )
    )
    if started is None:
        return "skipped"
    client = runtime.transport.client(bot.tg_bot_id)
    if client is None:
        return None  # the bot is not running right now
    c = await session.get(Collection, ev.collection_id)
    lang = lang_of(user.language)
    if c.scope == CollectionScope.PERSONAL:
        tz = ZoneInfo(user.timezone)
    else:
        owner = await session.get(User, bot.owner_id)
        tz = ZoneInfo(owner.timezone)
    text = t(
        lang,
        f"data.event.{ev.kind}",
        title=label_of(c.title, lang, c.key),
        record=record_lines(collection_schema(c), ev.data, lang, tz),
    )
    try:
        await runtime.sender.send_text(client, user.tg_user_id, text, group=False)
    except TelegramForbiddenError:
        return "skipped"  # they blocked the bot
    except Exception:
        log.warning("sending record event %s failed", ev.id, exc_info=True)
        return None
    return "sent"
```

`test_blocked_bot…` builds `TelegramForbiddenError(method=..., message=...)`; adjust to aiogram 3.31's constructor if it differs, and say so in the report.

- [ ] **Step 6: Jobs**

In `backend/src/obot/telegram/bootstrap.py`, import `deliver_data_events` and append to `services.jobs`:

```python
        asyncio.create_task(run_every("data-events", 15, lambda: deliver_data_events(runtime))),
```

In `backend/src/obot/data/jobs.py`, `purge_records` also purges sent events older than the same retention: call `purge_events(session, retention_days)` before the commit and log the count.

- [ ] **Step 7: Run tests, lint, commit**

Run: `cd backend && uv run pytest tests/test_data_event_delivery.py tests/test_data_chat.py tests/test_data_describe.py -q && uv run pytest -q && uv run ruff check . && uv run ruff format --check .`

```bash
git add backend/src/obot/data/describe.py backend/src/obot/telegram/data_chat.py backend/src/obot/telegram/data_events.py backend/src/obot/i18n.py backend/src/obot/telegram/bootstrap.py backend/src/obot/data/jobs.py backend/tests/test_data_event_delivery.py
git commit -m "Send record notifications and reminders through the send layer

Co-Authored-By: <your model> <noreply@anthropic.com>"
```

---

### Task 3: The factory designs them

**Files:**
- Modify: `backend/src/obot/factory/collections.py` (`CollectionDraft` switches, `to_events`, `from_collection`, `sync_collections`)
- Modify: `backend/src/obot/factory/designer.py` (instructions)
- Modify: `backend/src/obot/data/describe.py` (`events_text`), `backend/src/obot/telegram/factory.py` (card), `backend/src/obot/i18n.py`
- Test: `backend/tests/test_factory_collections.py`, `backend/tests/test_factory.py`, `backend/tests/test_data_describe.py`

**Interfaces:**
- Produces:
  - `DraftReminder(date_field: str, time_field: str | None = None, before_minutes: int)`.
  - New `CollectionDraft` fields, where `None` means "keep the current setting":
    - `notify_owner: bool | None` (shared: message the owner on each new record);
    - `notify_author_on_change: bool | None` (shared: tell the person when someone else changes or removes their record);
    - `reminder: DraftReminder | None`, plus `no_reminder: bool = False` to remove one.
  - `to_events(d: CollectionDraft, current: CollectionEvents) -> CollectionEvents`.
  - `describe.events_text(events: CollectionEvents, schema, lang) -> list[str]`.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_factory_collections.py`:

```python
from obot.data.events import collection_events
from obot.data.schema import CollectionEvents
from obot.factory.collections import DraftReminder, to_events


def test_to_events_keeps_what_the_draft_leaves_out() -> None:
    current = CollectionEvents.model_validate(
        {
            "notify": [{"on": "create", "to": "owner"}],
            "remind": [{"date_field": "date", "time_field": "time", "before_minutes": 1440}],
        }
    )
    unchanged = to_events(BOOKINGS, current)
    assert unchanged == current

    off = BOOKINGS.model_copy(update={"notify_owner": False, "no_reminder": True})
    assert to_events(off, current) == CollectionEvents()

    on = BOOKINGS.model_copy(
        update={
            "notify_author_on_change": True,
            "reminder": DraftReminder(date_field="date", time_field="time", before_minutes=60),
        }
    )
    events = to_events(on, CollectionEvents())
    assert {(n.on, n.to) for n in events.notify} == {("update", "author"), ("delete", "author")}
    assert events.remind[0].before_minutes == 60


async def test_sync_stores_events_and_reports_bad_ones(data_world) -> None:
    w = data_world
    draft = BOOKINGS.model_copy(
        update={
            "notify_owner": True,
            "reminder": DraftReminder(date_field="date", time_field="time", before_minutes=1440),
        }
    )
    assert await sync_collections(w.session, w.pricing, w.bot, [draft], "fa") == []
    [c] = await template_collections(w.session, w.bot.id)
    events = collection_events(c)
    assert [(n.on, n.to) for n in events.notify] == [("create", "owner")]
    assert from_collection(c, "fa").notify_owner is True

    personal = BOOKINGS.model_copy(
        update={"key": "notes", "title": "یادداشت", "scope": "personal", "notify_owner": True}
    )
    [problem] = await sync_collections(w.session, w.pricing, w.bot, [personal], "fa")
    assert problem.reason == "invalid"
```

Append to `backend/tests/test_data_describe.py`:

```python
from obot.data.describe import events_text
from obot.data.schema import CollectionEvents


def test_events_in_plain_words() -> None:
    events = CollectionEvents.model_validate(
        {
            "notify": [{"on": "create", "to": "owner"}, {"on": "update", "to": "author"}],
            "remind": [{"date_field": "date", "time_field": "time", "before_minutes": 1440}],
        }
    )
    assert events_text(events, BOOKINGS, "fa") == [
        "با هر مورد تازه به تو خبر می‌دهم",
        "اگر چیزی را تغییر دهی یا حذف کنی، به ثبت‌کننده خبر می‌دهم",
        "۱ روز قبل از تاریخ یادآوری می‌کنم",
    ]
    assert events_text(events, BOOKINGS, "en")[2] == "I remind them 1 day before Date"
```

Append to `backend/tests/test_factory.py` a card test: a `DENTIST` draft whose collection has `notify_owner=True` renders "با هر مورد تازه به تو خبر می‌دهم" on the fa card.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && uv run pytest tests/test_factory_collections.py tests/test_data_describe.py tests/test_factory.py -q`
Expected: FAIL (`DraftReminder` / `events_text` missing).

- [ ] **Step 3: Draft switches and conversion**

In `backend/src/obot/factory/collections.py` add:

```python
class DraftReminder(BaseModel):
    date_field: str
    time_field: str | None = Field(default=None, description="With a date field.")
    before_minutes: int = Field(description="e.g. 1440 for a day before, 60 for an hour.")
```

and to `CollectionDraft`:

```python
    notify_owner: bool | None = Field(
        default=None,
        description="Shared only: message the owner about each new record. Omit to keep.",
    )
    notify_author_on_change: bool | None = Field(
        default=None,
        description=(
            "Shared only: tell the person when someone else changes or removes their "
            "record. Omit to keep."
        ),
    )
    reminder: DraftReminder | None = Field(
        default=None, description="Remind the person before their record's date. Omit to keep."
    )
    no_reminder: bool = Field(default=False, description="True removes the reminder.")
```

and:

```python
def to_events(d: CollectionDraft, current: CollectionEvents) -> CollectionEvents:
    """The draft's switches over the current events; omitted switches keep their value."""
    notify = {(n.on, n.to) for n in current.notify}
    if d.notify_owner is not None:
        notify.discard(("create", "owner"))
        if d.notify_owner:
            notify.add(("create", "owner"))
    if d.notify_author_on_change is not None:
        notify -= {("update", "author"), ("delete", "author")}
        if d.notify_author_on_change:
            notify |= {("update", "author"), ("delete", "author")}
    remind = current.remind
    if d.no_reminder:
        remind = []
    elif d.reminder is not None:
        remind = [RemindRule(**d.reminder.model_dump())]
    order = ["create", "update", "delete"]
    return CollectionEvents(
        notify=[
            NotifyRule(on=on, to=to)
            for on, to in sorted(notify, key=lambda n: (order.index(n[0]), n[1]))
        ],
        remind=remind,
    )
```

In `from_collection`, set `notify_owner`, `notify_author_on_change` and `reminder` from `collection_events(c)`, so the designer sees the current values.

In `sync_collections`, after a successful create or schema update, inside the same savepoint, call `await set_collection_events(session, c, to_events(d, collection_events(c)))` with the created or updated collection `c`. Skip the call when the result equals the current events. An `InvalidSchema` from `check_events` becomes `Problem(d.title, "invalid")` through the existing handler.

- [ ] **Step 4: Card text**

`describe.events_text(events, schema, lang) -> list[str]` uses these i18n keys:

| Key | en | fa |
|---|---|---|
| `data.events.notify_owner` | "I tell you about each new one" | "با هر مورد تازه به تو خبر می‌دهم" |
| `data.events.notify_author` | "If you change or remove one, I tell the person who added it" | "اگر چیزی را تغییر دهی یا حذف کنی، به ثبت‌کننده خبر می‌دهم" |
| `data.events.remind` | "I remind them {amount} before {field}" | "{amount} قبل از {field} یادآوری می‌کنم" |
| `duration.days` | "{n} day(s)" | "{n} روز" |
| `duration.hours` | "{n} hour(s)" | "{n} ساعت" |
| `duration.minutes` | "{n} minute(s)" | "{n} دقیقه" |

- `notify_author` appears once when either the update or the delete author rule exists.
- `amount` uses the largest whole unit (days, then hours, then minutes), with Persian digits in fa.
- `field` is the date field's label.
- `collection_text` gains an optional `events: CollectionEvents | None = None` parameter and appends these lines after the rules.
- The factory card passes `to_events(c, CollectionEvents())` for each drafted collection.

For en, write "1 day"/"2 days" by passing `n` and choosing the singular or plural key (`duration.day`/`duration.days`, and so on). Keep the en test assertion "1 day".

- [ ] **Step 5: Designer instructions**

In `backend/src/obot/factory/designer.py`, replace the sentence "Collections cannot notify or remind anyone yet: never promise that." with:

```text
Collections can message people, with fixed texts and at no cost: set notify_owner on a
shared collection when the owner wants to hear about each new record (bookings, orders),
notify_author_on_change when people should hear about changes to their record, and a
reminder (date field, optional time field, minutes before) when people should be reminded
before their booking. Only shared collections can message the owner. Omit a switch to keep
its current value.
```

- [ ] **Step 6: Run tests, lint, commit**

Run: `cd backend && uv run pytest tests/test_factory*.py tests/test_data_*.py -q && uv run pytest -q && uv run ruff check . && uv run ruff format --check .`

```bash
git add backend/src/obot/factory/collections.py backend/src/obot/factory/designer.py backend/src/obot/data/describe.py backend/src/obot/telegram/factory.py backend/src/obot/i18n.py backend/tests/test_factory_collections.py backend/tests/test_factory.py backend/tests/test_data_describe.py
git commit -m "Let the factory turn on booking notifications and reminders

Co-Authored-By: <your model> <noreply@anthropic.com>"
```

---

## Spec coverage (P5-5)

| Spec ۸٫۸ requirement | Task |
|---|---|
| New record or change → message to the owner or the author | 1 (outbox), 2 (delivery), 3 (factory switches) |
| Reminder X before the date field | 1 (`RecordReminder`, recomputed on change), 2 |
| Templated, no model call, no cost, through the send layer | 2 |
| After commit; a failed send never undoes the write | 1 (outbox in the same transaction), 2 (retries/backoff) |
| Recipient who hasn't started the bot → only recorded | 2 (`skipped`) |
| Reminders separate from tasks, picked with SKIP LOCKED | 1, 2 |
| Owner never hears about personal records | 1 (`check_events`) |
| Card: «با هر نوبت جدید خبرت می‌کنم • یک روز قبل یادآوری می‌کنم» | 3 |
