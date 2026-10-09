"""Notifications and reminders about records (SPEC 8.8): an outbox written in the same
transaction as the record, and reminders the delivery job turns into outbox rows.

Only obot.data writes here; obot.telegram.data_events sends the messages.
"""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, or_, select
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
    Bot,
    Collection,
    CollectionScope,
    Record,
    RecordEvent,
    RecordReminder,
    User,
)

MAX_ATTEMPTS = 5
DATE_ONLY_AT = time(9, 0)  # reminders for a date without a time
# A reminder claimed this late (e.g. after downtime) is dropped once its record's moment has
# passed; an at-the-time reminder (before_minutes 0) gets this long.
LATE_GRACE = timedelta(minutes=10)


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


async def _zone(
    session: AsyncSession, c: Collection, record: Record, zones: dict[tuple[str, int], ZoneInfo]
) -> ZoneInfo:
    """The zone of the record's dates: its author's in a personal collection, the bot
    owner's otherwise. `zones` caches the lookups across records."""
    if c.scope == CollectionScope.PERSONAL:
        key = ("user", record.author_user_id)
        if key not in zones:
            zones[key] = ZoneInfo((await session.get(User, record.author_user_id)).timezone)
    else:
        key = ("bot", c.bot_id)
        if key not in zones:
            owner = await session.scalar(
                select(User).join(Bot, Bot.owner_id == User.id).where(Bot.id == c.bot_id)
            )
            zones[key] = ZoneInfo(owner.timezone)
    return zones[key]


async def _schedule(
    session: AsyncSession,
    c: Collection,
    record: Record,
    owner_id: int,
    tz: ZoneInfo,
    now: datetime,
    stored: dict | None = None,
) -> None:
    """Bring the record's reminders in line with the collection's remind rules. Sent rows
    stay sent unless their time changes; a due row the job has not claimed yet is kept.
    `stored` (the record's rows by rule) comes from callers that batch-loaded them and have
    already dropped the unsent rows of removed rules."""
    rules = collection_events(c).remind
    schema = _schema(c)
    unsent = RecordReminder.sent_at.is_(None)
    if stored is None:
        stored = {
            row.rule: row
            for row in await session.execute(
                _stored_rows().where(RecordReminder.record_id == record.id)
            )
        }
        await session.execute(
            delete(RecordReminder).where(
                RecordReminder.record_id == record.id, RecordReminder.rule >= len(rules), unsent
            )
        )
    for i, rule in enumerate(rules):
        moment = moment_of(rule, record.data, schema, tz)
        fire_at = None if moment is None else moment - timedelta(minutes=rule.before_minutes)
        old = stored.get(i)
        if fire_at is None or fire_at <= now:
            if old is not None and old.sent_at is None and old.fire_at != fire_at:
                await session.execute(
                    delete(RecordReminder).where(
                        RecordReminder.record_id == record.id, RecordReminder.rule == i, unsent
                    )
                )
            continue
        recipient = _recipient(owner_id, record, rule.to)
        if old is not None and (old.fire_at, old.recipient_user_id) == (fire_at, recipient):
            continue  # unchanged: a sent row stays sent, an unsent one stays armed
        stmt = insert(RecordReminder).values(
            record_id=record.id,
            collection_id=c.id,
            rule=i,
            recipient_user_id=recipient,
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


def _stored_rows():
    return select(
        RecordReminder.record_id,
        RecordReminder.rule,
        RecordReminder.fire_at,
        RecordReminder.sent_at,
        RecordReminder.recipient_user_id,
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
        if rule.on != kind or (rule.to == "owner" and c.scope != CollectionScope.SHARED):
            continue  # owners are told only about shared collections (check_events)
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
    """Recompute reminders of live records after the collection's events changed. Records
    whose dates are all past are left alone: no reminder of theirs can fire any more (one
    still stored is dropped when claimed)."""
    rules = collection_events(c).remind
    unsent = RecordReminder.sent_at.is_(None)
    await session.execute(
        delete(RecordReminder).where(
            RecordReminder.collection_id == c.id, RecordReminder.rule >= len(rules), unsent
        )
    )
    if not rules:
        return
    # Dates are stored as ISO text, so a text compare finds them; a day of slack covers
    # every time zone.
    since = (now - timedelta(days=1)).date().isoformat()
    upcoming = or_(*(Record.data[f].astext >= since for f in {r.date_field for r in rules}))
    records = list(
        await session.scalars(
            select(Record).where(
                Record.collection_id == c.id, Record.deleted_at.is_(None), upcoming
            )
        )
    )
    stored: dict[int, dict] = {}
    for row in await session.execute(
        _stored_rows().where(RecordReminder.collection_id == c.id, RecordReminder.rule < len(rules))
    ):
        stored.setdefault(row.record_id, {})[row.rule] = row
    zones = {("bot", c.bot_id): ZoneInfo(owner.timezone)}
    for record in records:
        tz = await _zone(session, c, record, zones)
        await _schedule(session, c, record, owner.id, tz, now, stored.get(record.id, {}))
    await session.flush()


async def claim_due_reminders(session: AsyncSession, now: datetime, limit: int = 100) -> int:
    """Turn due reminders into outbox rows (once each); the caller commits. A reminder whose
    record's moment has already passed (the job was down) is marked without a message.
    Returns how many reminders were claimed."""
    due = list(
        await session.scalars(
            select(RecordReminder)
            .where(RecordReminder.sent_at.is_(None), RecordReminder.fire_at <= now)
            .order_by(RecordReminder.fire_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    zones: dict[tuple[str, int], ZoneInfo] = {}
    for rem in due:
        rem.sent_at = now
        record = await session.get(Record, rem.record_id)
        if record is None or record.deleted_at is not None:
            continue
        c = await session.get(Collection, rem.collection_id)
        rules = collection_events(c).remind
        if rem.rule >= len(rules):
            continue
        rule = rules[rem.rule]
        moment = moment_of(rule, record.data, _schema(c), await _zone(session, c, record, zones))
        if moment is None:
            continue
        fire_at = moment - timedelta(minutes=rule.before_minutes)
        if now > max(moment, fire_at + LATE_GRACE):
            continue  # too late: the booking has passed
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


def postpone(ev: RecordEvent, now: datetime) -> None:
    """Try again in a minute without counting an attempt (e.g. the bot is not running)."""
    ev.next_attempt_at = now + timedelta(minutes=1)


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
