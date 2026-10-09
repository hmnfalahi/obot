"""Notifications and reminders about records (SPEC 8.8): an outbox written in the same
transaction as the record, and reminders the delivery job turns into outbox rows.

Only obot.data writes here; obot.telegram.data_events sends the messages.
"""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from obot.data.schema import (
    CollectionEvents,
    CollectionSchema,
    FieldType,
    RemindRule,
    remind_fits,
)
from obot.db.models import (
    Collection,
    CollectionScope,
    Record,
    RecordEvent,
    RecordReminder,
    User,
)

MAX_ATTEMPTS = 5
DATE_ONLY_AT = time(9, 0)  # reminders for a date without a time


def collection_events(c: Collection) -> CollectionEvents:
    return CollectionEvents.model_validate(c.events or {})


def _schema(c: Collection) -> CollectionSchema:
    return CollectionSchema.model_validate({"fields": c.schema["fields"], "rules": c.rules})


def moment_of(
    rule: RemindRule, data: dict, schema: CollectionSchema, tz: ZoneInfo
) -> datetime | None:
    """The record's moment for a remind rule, or None when it has no usable date (also when
    the rule no longer fits the schema or a stored value does not parse)."""
    if not remind_fits(rule, schema):
        return None
    raw = data.get(rule.date_field)
    if raw is None:
        return None
    try:
        if schema.field(rule.date_field).type == FieldType.DATETIME:
            moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return moment.astimezone(UTC) if moment.tzinfo else None
        at = DATE_ONLY_AT
        if rule.time_field is not None:
            if data.get(rule.time_field) is None:
                return None
            at = time.fromisoformat(data[rule.time_field])
        return datetime.combine(date.fromisoformat(raw), at, tzinfo=tz).astimezone(UTC)
    except (TypeError, ValueError, AttributeError):
        return None


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
    """Bring the record's reminders in line with the collection's remind rules. Sent rows
    stay sent unless their time changes; a due row the job has not claimed yet is kept."""
    rules = collection_events(c).remind
    schema = _schema(c)
    stored = {
        row.rule: row
        for row in await session.execute(
            select(RecordReminder.rule, RecordReminder.fire_at, RecordReminder.sent_at).where(
                RecordReminder.record_id == record.id
            )
        )
    }
    unsent = RecordReminder.sent_at.is_(None)
    await session.execute(
        delete(RecordReminder).where(
            RecordReminder.record_id == record.id, RecordReminder.rule >= len(rules), unsent
        )
    )
    for i, rule in enumerate(rules):
        moment = moment_of(rule, record.data, schema, tz)
        fire_at = None if moment is None else moment - timedelta(minutes=rule.before_minutes)
        if fire_at is None or fire_at <= now:
            old = stored.get(i)
            if old is not None and old.sent_at is None and old.fire_at != fire_at:
                await session.execute(
                    delete(RecordReminder).where(
                        RecordReminder.record_id == record.id, RecordReminder.rule == i, unsent
                    )
                )
            continue
        stmt = insert(RecordReminder).values(
            record_id=record.id,
            collection_id=c.id,
            rule=i,
            recipient_user_id=_recipient(owner_id, record, rule.to),
            fire_at=fire_at,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[RecordReminder.record_id, RecordReminder.rule],
            set_={
                "fire_at": stmt.excluded.fire_at,
                "recipient_user_id": stmt.excluded.recipient_user_id,
                "sent_at": None,
            },
            where=RecordReminder.sent_at.is_(None)
            | (stmt.excluded.fire_at != RecordReminder.fire_at),
        )
        await session.scalars(
            stmt.returning(RecordReminder), execution_options={"populate_existing": True}
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
        await session.execute(delete(RecordReminder).where(RecordReminder.record_id == record.id))
    else:
        await _schedule(session, c, record, owner_id, tz, now)
    await session.flush()


async def reschedule_collection(
    session: AsyncSession, c: Collection, owner: User, now: datetime
) -> None:
    """Recompute reminders of every live record after the collection's events changed."""
    rules = collection_events(c).remind
    unsent = RecordReminder.sent_at.is_(None)
    await session.execute(
        delete(RecordReminder).where(
            RecordReminder.collection_id == c.id, RecordReminder.rule >= len(rules), unsent
        )
    )
    if not rules:
        return
    records = await session.scalars(
        select(Record).where(Record.collection_id == c.id, Record.deleted_at.is_(None))
    )
    zones: dict[int, ZoneInfo] = {}
    for record in list(records):
        if c.scope == CollectionScope.PERSONAL:
            if record.author_user_id not in zones:
                author = await session.get(User, record.author_user_id)
                zones[record.author_user_id] = ZoneInfo(author.timezone)
            tz = zones[record.author_user_id]
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
    result = await session.execute(delete(RecordEvent).where(RecordEvent.sent_at < cutoff))
    return result.rowcount
