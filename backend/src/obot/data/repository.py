"""Collections and record writes. Every record access takes a DataSpace (obot.data.space);
do not query Collection, Record or RecordKey anywhere else."""

import re

from sqlalchemy import delete, exists, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from obot.billing.plans import Pricing
from obot.data.errors import CollectionNotFound, InvalidSchema, LimitReached, NotAllowed
from obot.data.schema import KEY_PATTERN, CollectionSchema, check_evolution
from obot.data.space import DataSpace, visible
from obot.db.models import Bot, Collection, CollectionScope, Record, RecordKey, User


def collection_schema(c: Collection) -> CollectionSchema:
    return CollectionSchema.model_validate({"fields": c.schema["fields"], "rules": c.rules})


def _store_schema(c: Collection, schema: CollectionSchema) -> None:
    dumped = schema.model_dump(mode="json")
    c.schema = {"fields": dumped["fields"]}
    c.rules = dumped["rules"]


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
    # A template key clashes with template keys; a personal key also with the template's,
    # so a person's own collection never shadows a template one.
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
    rows = await session.scalars(
        select(Collection)
        .where(
            Collection.bot_id == space.bot_id,
            Collection.created_by_user_id.is_(None)
            | (Collection.created_by_user_id == space.user_id),
        )
        .order_by(Collection.id)
    )
    return [c for c in rows if visible(space, c)]


async def get_collection(session: AsyncSession, space: DataSpace, key: str) -> Collection:
    for c in await list_collections(session, space):
        if c.key == key:
            return c
    raise CollectionNotFound(key)


async def update_collection_schema(
    session: AsyncSession, c: Collection, new: CollectionSchema
) -> None:
    """Replace the schema; additive changes only once the collection has records.
    Callers check that the caller may edit the collection (owner, or its personal creator)."""
    has_records = await session.scalar(
        select(exists().where(Record.collection_id == c.id, Record.deleted_at.is_(None)))
    )
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
