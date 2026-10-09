"""Collections and record writes. Every record access takes a DataSpace (obot.data.space);
do not query Collection, Record or RecordKey anywhere else."""

import json
import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, exists, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from obot.billing.plans import Pricing
from obot.data.errors import (
    CollectionNotFound,
    DuplicateRecord,
    InvalidSchema,
    LimitReached,
    NotAllowed,
    RecordNotFound,
    StaleRecord,
)
from obot.data.events import on_record_change, reschedule_collection
from obot.data.schema import (
    KEY_PATTERN,
    CollectionEvents,
    CollectionSchema,
    check_allowed_hours,
    check_events,
    check_evolution,
    unique_keys,
    validate_record,
)
from obot.data.space import (
    DataSpace,
    can_change,
    payer,
    record_scope_key,
    require_visible,
    row_filter,
    tz_for,
    visible,
)
from obot.db.models import Bot, Collection, CollectionScope, Record, RecordKey, User

MAX_ID = 2**63 - 1  # the largest id a record or collection can have


def collection_schema(c: Collection) -> CollectionSchema:
    return CollectionSchema.model_validate({"fields": c.schema["fields"], "rules": c.rules})


def _store_schema(c: Collection, schema: CollectionSchema) -> None:
    dumped = schema.model_dump(mode="json")
    c.schema = {"fields": dumped["fields"]}
    c.rules = dumped["rules"]


def check_shared_unique(scope: CollectionScope, schema: CollectionSchema) -> None:
    """In a shared collection a duplicate error shows other people's values, so every field
    of a unique rule must be public."""
    if scope != CollectionScope.SHARED:
        return
    for group in schema.rules.unique:
        for key in group:
            if not schema.field_map()[key].public:
                raise InvalidSchema(
                    f"{key}: fields of a unique rule in a shared collection must be public"
                )


async def create_collection(
    session: AsyncSession,
    pricing: Pricing,
    *,
    bot: Bot,
    key: str,
    title: dict[str, str],
    scope: CollectionScope,
    schema: CollectionSchema,
    created_by: User | None = None,
) -> Collection:
    """A template collection (created_by None, counted against the owner's plan), or a
    personal one a person made in chat (counted against theirs). Callers check that the
    caller is the owner for template collections."""
    if created_by is not None and scope != CollectionScope.PERSONAL:
        raise NotAllowed("only personal collections can be made in chat")
    if not re.fullmatch(KEY_PATTERN, key):
        raise InvalidSchema(f"{key!r} is not a valid collection key")
    check_shared_unique(scope, schema)
    plan_user = created_by or await session.get(User, bot.owner_id)
    count = await session.scalar(
        select(func.count(Collection.id)).where(
            Collection.bot_id == bot.id,
            Collection.created_by_user_id.is_(None)
            if created_by is None
            else Collection.created_by_user_id == created_by.id,
        )
    )
    if count >= pricing.plan(plan_user.plan).max_data_collections:
        raise LimitReached("collections")
    # A template key clashes with template keys; a personal key also with the template's and
    # the person's own. A template made later can still share a key with an existing personal
    # collection; list_collections then hides the personal one.
    owners = [Collection.created_by_user_id.is_(None)]
    if created_by is not None:
        owners.append(Collection.created_by_user_id == created_by.id)
    taken = await session.scalar(
        select(exists().where(Collection.bot_id == bot.id, Collection.key == key, or_(*owners)))
    )
    if taken:
        raise InvalidSchema(f"a collection named {key!r} exists")
    c = Collection(
        bot_id=bot.id,
        key=key,
        title=title,
        scope=scope,
        created_by_user_id=created_by.id if created_by else None,
    )
    _store_schema(c, schema)
    try:
        async with session.begin_nested():
            session.add(c)
            await session.flush()
    except IntegrityError:
        raise InvalidSchema(f"a collection named {key!r} exists") from None
    return c


async def list_collections(session: AsyncSession, space: DataSpace) -> list[Collection]:
    """Template collections first, then the person's own; a personal collection whose key a
    template also uses is hidden, so the template wins. Each group is ordered by id."""
    rows = await session.scalars(
        select(Collection)
        .where(
            Collection.bot_id == space.bot_id,
            Collection.created_by_user_id.is_(None)
            | (Collection.created_by_user_id == space.user_id),
        )
        .order_by(Collection.id)
    )
    seen = [c for c in rows if visible(space, c)]
    templates = [c for c in seen if c.created_by_user_id is None]
    taken = {c.key for c in templates}
    return templates + [c for c in seen if c.created_by_user_id is not None and c.key not in taken]


async def template_collections(session: AsyncSession, bot_id: int) -> list[Collection]:
    """The bot's own collections (its template), for the factory and the owner's panel."""
    rows = await session.scalars(
        select(Collection)
        .where(Collection.bot_id == bot_id, Collection.created_by_user_id.is_(None))
        .order_by(Collection.id)
    )
    return list(rows)


async def get_collection(session: AsyncSession, space: DataSpace, key: str) -> Collection:
    for c in await list_collections(session, space):
        if c.key == key:
            return c
    raise CollectionNotFound(key)


async def collection_by_id(
    session: AsyncSession, space: DataSpace, collection_id: int
) -> Collection:
    """A collection by id, e.g. from a button; only if the space can see it."""
    c = await session.get(Collection, collection_id)
    if c is None or not visible(space, c):
        raise CollectionNotFound(str(collection_id))
    return c


async def lock_collection(
    session: AsyncSession, space: DataSpace, collection_id: int
) -> Collection:
    """Like collection_by_id, but reads the current row and locks it until the transaction
    ends, so concurrent schema changes are serialized."""
    c = await session.get(Collection, collection_id, with_for_update=True, populate_existing=True)
    if c is None or not visible(space, c):
        raise CollectionNotFound(str(collection_id))
    return c


async def update_collection_schema(
    session: AsyncSession, c: Collection, new: CollectionSchema
) -> None:
    """Replace the schema; additive changes only once the collection has records.
    Callers check that the caller may edit the collection (owner, or its personal creator)."""
    has_records = await session.scalar(
        select(exists().where(Record.collection_id == c.id, Record.deleted_at.is_(None)))
    )
    check_shared_unique(c.scope, new)
    check_evolution(collection_schema(c), new, has_records=has_records)
    _store_schema(c, new)
    c.schema_version += 1
    kept = [",".join(group) for group in new.rules.unique]
    await session.execute(
        delete(RecordKey).where(RecordKey.collection_id == c.id, RecordKey.rule.not_in(kept))
    )
    await session.flush()


async def delete_collection(session: AsyncSession, c: Collection) -> None:
    """Delete a collection with all its records."""
    await session.execute(delete(Collection).where(Collection.id == c.id))
    session.expunge(c)


async def get_record(
    session: AsyncSession, space: DataSpace, c: Collection, record_id: int
) -> Record:
    require_visible(space, c)
    record = await session.scalar(
        select(Record).where(Record.id == record_id, *row_filter(space, c))
    )
    if record is None:
        raise RecordNotFound(record_id)
    return record


def _check_size(pricing: Pricing, space: DataSpace, c: Collection, data: dict) -> None:
    size = len(json.dumps(data, ensure_ascii=False).encode())
    if size > pricing.plan(payer(space, c)[1]).max_record_bytes:
        raise LimitReached("record_bytes")


async def _check_record_count(
    session: AsyncSession, pricing: Pricing, space: DataSpace, c: Collection
) -> None:
    # Checked before the insert, so concurrent writers may overshoot by a record or two.
    user_id, plan = payer(space, c)
    query = (
        select(func.count(Record.id))
        .join(Collection, Collection.id == Record.collection_id)
        .where(Record.bot_id == c.bot_id, Record.deleted_at.is_(None))
    )
    if c.scope == CollectionScope.PERSONAL:
        query = query.where(
            Collection.scope == CollectionScope.PERSONAL, Record.author_user_id == user_id
        )
    else:
        query = query.where(Collection.scope != CollectionScope.PERSONAL)
    if await session.scalar(query) >= pricing.plan(plan).max_data_records:
        raise LimitReached("records")


async def _claim_keys(
    session: AsyncSession, c: Collection, record: Record, schema: CollectionSchema
) -> None:
    """Insert the record's unique keys; a key someone else holds is a duplicate. A concurrent
    holder that has not committed yet makes Postgres wait for it, so exactly one writer wins."""
    for rule, value_hash in unique_keys(schema, record.data):
        claimed = await session.scalar(
            insert(RecordKey)
            .values(
                collection_id=c.id,
                rule=rule,
                scope_key=record_scope_key(c, record),
                value_hash=value_hash,
                record_id=record.id,
            )
            .on_conflict_do_nothing()
            .returning(RecordKey.rule)
        )
        if claimed is None:
            raise DuplicateRecord(rule.split(","))


async def add_record(
    session: AsyncSession, pricing: Pricing, space: DataSpace, c: Collection, data: dict
) -> Record:
    require_visible(space, c)
    schema = collection_schema(c)
    tz = tz_for(space, c)
    values = validate_record(schema, data, tz)
    check_allowed_hours(schema, values, tz)
    _check_size(pricing, space, c, values)
    await _check_record_count(session, pricing, space, c)
    record = Record(
        collection_id=c.id,
        bot_id=c.bot_id,
        author_user_id=space.user_id,
        tg_chat_id=space.tg_chat_id if c.scope == CollectionScope.GROUP else None,
        data=values,
        schema_version=c.schema_version,
    )
    async with session.begin_nested():
        session.add(record)
        await session.flush()
        await _claim_keys(session, c, record, schema)
    await on_record_change(
        session,
        c,
        record,
        "create",
        owner_id=space.owner_id,
        actor_user_id=space.user_id,
        tz=tz,
        now=datetime.now(UTC),
    )
    return record


async def _locked_record(
    session: AsyncSession, space: DataSpace, c: Collection, record_id: int
) -> Record:
    require_visible(space, c)
    record = await session.scalar(
        select(Record).where(Record.id == record_id, *row_filter(space, c)).with_for_update()
    )
    if record is None:
        raise RecordNotFound(record_id)
    if not can_change(space, c, record):
        raise NotAllowed("only the author can change this record")
    return record


async def update_record(
    session: AsyncSession,
    pricing: Pricing,
    space: DataSpace,
    c: Collection,
    record_id: int,
    changes: dict,
    *,
    expected_version: int | None = None,
) -> Record:
    """Merge `changes` into the record. The row is locked, so concurrent updates never lose
    each other's fields; `expected_version` (from the panel) refuses edits of a stale copy.
    After an error the passed or returned ORM object may be expired: keep the record id and
    fetch the record again."""
    record = await _locked_record(session, space, c, record_id)
    if expected_version is not None and record.version != expected_version:
        raise StaleRecord()
    schema = collection_schema(c)
    tz = tz_for(space, c)
    values = validate_record(schema, changes, tz, current=record.data)
    check_allowed_hours(schema, values, tz)
    _check_size(pricing, space, c, values)
    async with session.begin_nested():
        await session.execute(delete(RecordKey).where(RecordKey.record_id == record.id))
        record.data = values
        record.version += 1
        record.schema_version = c.schema_version
        await session.flush()
        await _claim_keys(session, c, record, schema)
    await on_record_change(
        session,
        c,
        record,
        "update",
        owner_id=space.owner_id,
        actor_user_id=space.user_id,
        tz=tz,
        now=datetime.now(UTC),
    )
    return record


async def delete_record(
    session: AsyncSession, space: DataSpace, c: Collection, record_id: int
) -> None:
    """Soft delete: the record disappears now and is purged later; its unique keys are freed."""
    record = await _locked_record(session, space, c, record_id)
    record.deleted_at = datetime.now(UTC)
    await session.execute(delete(RecordKey).where(RecordKey.record_id == record.id))
    await on_record_change(
        session,
        c,
        record,
        "delete",
        owner_id=space.owner_id,
        actor_user_id=space.user_id,
        tz=tz_for(space, c),
        now=datetime.now(UTC),
    )


async def was_deleted(
    session: AsyncSession, space: DataSpace, c: Collection, record_id: int
) -> bool:
    """Whether `record_id` is a soft-deleted record of `c` this space could have read and
    changed, i.e. whose delete it may be told about."""
    require_visible(space, c)
    record = await session.scalar(
        select(Record).where(Record.id == record_id, *row_filter(space, c, deleted=True))
    )
    return record is not None and can_change(space, c, record)


async def purge_deleted_records(session: AsyncSession, retention_days: int) -> int:
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    result = await session.execute(delete(Record).where(Record.deleted_at < cutoff))
    return result.rowcount


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
