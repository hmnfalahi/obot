"""The data_* tools' access to one bot's collections on behalf of one person, in a private
context: their chat with the bot, their task, or another bot's question for them.

Each call opens its own session and commits writes at once, so no row locks or unique keys
are held while the model is thinking, and a booking is saved even if the reply fails later.
"""

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from obot.billing.plans import Pricing
from obot.data.errors import (
    CollectionNotFound,
    DataError,
    InvalidQuery,
    InvalidSchema,
    NotAllowed,
    RecordNotFound,
)
from obot.data.format import (
    describe_collections,
    describe_error,
    format_records,
    format_summary,
    format_taken,
)
from obot.data.query import Filter, FilterOp, Metric, Period, find_records, summarize
from obot.data.query import availability as find_taken
from obot.data.repository import (
    MAX_ID,
    add_record,
    collection_schema,
    create_collection,
    get_collection,
    get_record,
    list_collections,
    lock_collection,
    update_collection_schema,
    update_record,
)
from obot.data.schema import CollectionSchema
from obot.data.space import DataSpace, can_change, space_for, tz_for
from obot.db.models import Bot, Collection, CollectionScope, Record, User
from obot.i18n import lang_of

NoticeShared = Callable[[], Awaitable[None]]
ConfirmDelete = Callable[[Collection, Record], Awaitable[str]]


@dataclass(frozen=True)
class Definition:
    """A personal collection a person is asked to confirm: a new one (collection_id None) or
    new fields for one of theirs. Stored as JSON until they press Save."""

    bot_id: int
    user_id: int
    key: str
    title: dict[str, str]
    schema: dict[str, Any]  # CollectionSchema as JSON
    collection_id: int | None
    schema_version: int | None  # the collection's version when proposed; None for a new one
    added: list[str]  # keys of new fields, or of choice fields that got new options

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str | bytes) -> "Definition":
        return cls(**json.loads(raw))


ProposeDefine = Callable[[Definition], Awaitable[str]]

FIELD_SPEC_KEYS = {"key", "label", "type", "required", "options", "currency"}


def parse_field_specs(fields: Any, lang: str) -> list[dict[str, Any]]:
    """The model's field list as FieldDef JSON, labels in the person's language."""
    if not isinstance(fields, list) or not fields:
        raise InvalidSchema("fields must be a non-empty list of {key, label, type}")
    specs = []
    for item in fields:
        if not isinstance(item, dict) or not {"key", "label", "type"} <= item.keys():
            raise InvalidSchema("each field needs key, label and type")
        spec = {k: v for k, v in item.items() if k in FIELD_SPEC_KEYS and v is not None}
        if "options" in spec:
            options = spec["options"]
            if not isinstance(options, list) or not all(isinstance(o, str) for o in options):
                raise InvalidSchema(f"{item['key']}: options must be a list of strings")
            spec["options"] = [o.strip() for o in options]
        spec["label"] = {lang: str(item["label"]).strip() or str(item["key"])}
        specs.append(spec)
    return specs


def _schema(data: dict[str, Any]) -> CollectionSchema:
    try:
        return CollectionSchema.model_validate(data)
    except ValidationError as e:
        reasons = "; ".join(err["msg"].removeprefix("Value error, ") for err in e.errors())
        raise InvalidSchema(reasons) from None


def _merge(old: CollectionSchema, specs: list[dict[str, Any]]) -> tuple[dict, list[str]]:
    """Old fields plus new ones; choice fields gain new options. Returns (schema, added)."""
    merged = old.model_dump(mode="json")
    by_key = {f["key"]: f for f in merged["fields"]}
    added = []
    for spec in specs:
        current = by_key.get(spec["key"])
        if current is None:
            merged["fields"].append(spec)
            added.append(spec["key"])
            continue
        if current["type"] != spec["type"]:
            raise InvalidSchema(f"{spec['key']!r} already exists as {current['type']}")
        if current.get("hidden"):
            raise InvalidSchema(
                f"{spec['key']!r} was removed from this list and can't be added back"
            )
        new_options = [o for o in spec.get("options", []) if o not in current["options"]]
        if current["type"] == "choice" and new_options:
            current["options"] += new_options
            added.append(spec["key"])
    return merged, added


log = logging.getLogger(__name__)

# Postgres: deadlock detected, serialization failure.
RETRYABLE = {"40P01", "40001"}


def parse_filters(raw: list[dict[str, Any]] | None) -> list[Filter]:
    filters = []
    for item in raw or []:
        try:
            filters.append(Filter(str(item["field"]), FilterOp(item["op"]), item.get("value")))
        except (KeyError, TypeError, ValueError):
            ops = ", ".join(op.value for op in FilterOp)
            raise InvalidQuery(f"filters are {{field, op, value}} with op one of: {ops}") from None
    return filters


def _choice(enum_type, value: str | None, what: str):
    if value is None:
        return None
    try:
        return enum_type(value)
    except ValueError:
        options = ", ".join(m.value for m in enum_type)
        raise InvalidQuery(f"{what} must be one of: {options}") from None


def _sqlstate(e: DBAPIError) -> str | None:
    return getattr(e.orig, "sqlstate", None) or getattr(e.orig, "pgcode", None)


def _hidden_keys(c: Collection) -> list[str]:
    return [f.key for f in collection_schema(c).fields if f.hidden]


def _visible_data(c: Collection, record: Record) -> str:
    hidden = set(_hidden_keys(c))
    data = {k: v for k, v in record.data.items() if k not in hidden}
    return json.dumps(data, ensure_ascii=False)


def _check_object(value: Any) -> None:
    if not isinstance(value, dict):
        raise InvalidQuery("data must be an object of field: value")


def _check_id(record_id: Any) -> None:
    if not isinstance(record_id, int) or isinstance(record_id, bool):
        raise RecordNotFound(record_id)
    if not 1 <= record_id <= MAX_ID:
        raise RecordNotFound(record_id)


class DataService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        pricing: Pricing,
        bot_id: int,
        user_id: int,
        *,
        notice_shared: NoticeShared | None = None,
        confirm_delete: ConfirmDelete | None = None,
        propose_define: ProposeDefine | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._pricing = pricing
        self._bot_id = bot_id
        self._user_id = user_id
        self._notice_shared = notice_shared
        self._confirm_delete = confirm_delete
        self._propose_define = propose_define

    async def _space(self, session: AsyncSession, key: str = "") -> DataSpace:
        bot = await session.get(Bot, self._bot_id)
        user = await session.get(User, self._user_id)
        if bot is None or user is None:
            raise CollectionNotFound(key)
        return await space_for(session, bot, user)

    async def note(
        self, session: AsyncSession, now: datetime | None = None, *, can_define: bool = False
    ) -> str | None:
        """The catalog for the bot's instructions: None when there is nothing to say (no
        collections here and no personal_data switch) or the catalog cannot be read. It runs
        in a savepoint of the caller's session, so a failure never breaks the reply."""
        try:
            async with session.begin_nested():
                space = await self._space(session)
                collections = await list_collections(session, space)
            if not collections and not can_define:
                return None
            return describe_collections(
                collections, space, now or datetime.now(UTC), can_define=can_define
            )
        except Exception:
            log.exception("data catalog failed")
            return None

    async def _run(self, key: str, action, *, write: bool = False) -> str:
        """Run `action(session, space, c)`, which returns text, or (text, after) where `after`
        is an async callable run once the session is closed and writes are committed. A failing
        `after` is logged and never changes the result."""
        try:
            async with self._sessionmaker() as session:
                space = await self._space(session, key)
                c = await get_collection(session, space, str(key).strip())
                result = await action(session, space, c)
                if write:
                    await session.commit()
        except DataError as e:
            return describe_error(e)
        except DBAPIError as e:
            if _sqlstate(e) in RETRYABLE:
                return "ERROR: someone changed this at the same moment; try again."
            log.exception("data tool database error")
            return "ERROR: the data could not be read or saved; try again later."
        except Exception:
            log.exception("data tool failed")
            return "ERROR: the data could not be read or saved; try again later."
        text, after = result if isinstance(result, tuple) else (result, None)
        if after is not None:
            try:
                return await after()
            except Exception:
                log.exception("data tool follow-up failed")
                return "ERROR: could not ask the user to confirm; try again later."
        return text

    async def find(
        self,
        collection: str,
        filters: list[dict[str, Any]] | None = None,
        sort: str | None = None,
        descending: bool = True,
        limit: int = 20,
        availability: bool = False,
    ) -> str:
        async def action(session, space, c):
            parsed = parse_filters(filters)
            if availability:
                return format_taken(await find_taken(session, space, c, parsed, limit=limit))
            records = await find_records(
                session, space, c, parsed, sort=sort, descending=descending, limit=limit
            )
            return format_records(records, tz_for(space, c), hidden=_hidden_keys(c))

        return await self._run(collection, action)

    async def add(self, collection: str, data: dict[str, Any]) -> str:
        async def action(session, space, c):
            _check_object(data)
            record = await add_record(session, self._pricing, space, c, data)
            text = f"OK: saved as #{record.id}: {_visible_data(c, record)}"
            notice = self._notice_shared
            if c.scope == CollectionScope.SHARED and not space.is_owner and notice is not None:

                async def after() -> str:
                    try:
                        await notice()
                    except Exception:
                        log.exception("shared-data notice failed")
                    return text

                return text, after
            return text

        return await self._run(collection, action, write=True)

    async def update(self, collection: str, record_id: int, changes: dict[str, Any]) -> str:
        async def action(session, space, c):
            _check_object(changes)
            _check_id(record_id)
            record = await update_record(session, self._pricing, space, c, record_id, changes)
            return f"OK: #{record.id} is now {_visible_data(c, record)}"

        return await self._run(collection, action, write=True)

    async def delete(self, collection: str, record_id: int) -> str:
        if self._confirm_delete is None:
            return "ERROR: deleting needs the user's confirmation in their private chat."

        async def action(session, space, c):
            _check_id(record_id)
            record = await get_record(session, space, c, record_id)
            if not can_change(space, c, record):
                raise NotAllowed("this user cannot delete this record")
            _ = (record.data, c.title, c.schema)  # loaded before the session closes
            confirm = self._confirm_delete

            async def after() -> str:
                return await confirm(c, record)

            return "", after

        return await self._run(collection, action)

    async def summarize(
        self,
        collection: str,
        metric: str,
        field: str | None = None,
        filters: list[dict[str, Any]] | None = None,
        group_by: str | None = None,
        period: str | None = None,
    ) -> str:
        async def action(session, space, c):
            chosen = _choice(Metric, metric, "metric")
            rows = await summarize(
                session,
                space,
                c,
                chosen,
                field,
                filters=parse_filters(filters),
                group_by=group_by,
                period=_choice(Period, period, "period"),
            )
            return format_summary(rows, chosen)

        return await self._run(collection, action)

    async def _definition(
        self, session: AsyncSession, key: str, title: str, fields: Any
    ) -> Definition:
        bot = await session.get(Bot, self._bot_id)
        user = await session.get(User, self._user_id)
        if bot is None or user is None:
            raise CollectionNotFound(key)
        space = await space_for(session, bot, user)
        lang = lang_of(user.language)
        specs = parse_field_specs(fields, lang)
        key = str(key).strip()
        visible = await list_collections(session, space)
        own = next((c for c in visible if c.key == key and c.created_by_user_id == user.id), None)
        if own is None and any(c.key == key for c in visible):
            raise InvalidSchema(
                f"{key!r} is one of the bot's own collections; add records to it instead"
            )
        if own is not None:
            version = own.schema_version
            merged, added = _merge(collection_schema(own), specs)
            if not added:
                raise InvalidSchema(f"nothing new to add to {key!r}")
            schema = _schema(merged)
            # Dry run: the same checks Save will run (evolution, unique rules).
            await update_collection_schema(session, own, schema)
            return Definition(
                bot.id,
                user.id,
                key,
                dict(own.title),
                schema.model_dump(mode="json"),
                own.id,
                version,
                added,
            )
        schema = _schema({"fields": specs})
        title_text = str(title).strip() or key
        # Dry run: key format, clashes and the person's collection limit.
        await create_collection(
            session,
            self._pricing,
            bot=bot,
            key=key,
            title={lang: title_text},
            scope=CollectionScope.PERSONAL,
            schema=schema,
            created_by=user,
        )
        return Definition(
            bot.id,
            user.id,
            key,
            {lang: title_text},
            schema.model_dump(mode="json"),
            None,
            None,
            [f.key for f in schema.fields],
        )

    async def define(self, collection: str, title: str, fields: list[dict[str, Any]]) -> str:
        """Propose a personal collection (or new fields for one of the person's own); nothing
        changes until they press Save (apply_definition)."""
        if self._propose_define is None:
            return "ERROR: new collections need the user's confirmation in their private chat."
        try:
            async with self._sessionmaker() as session:
                try:
                    definition = await self._definition(session, collection, title, fields)
                finally:
                    await session.rollback()  # the checks above are a dry run
        except DataError as e:
            return describe_error(e)
        except Exception:
            log.exception("data_define failed")
            return "ERROR: the collection could not be prepared; try again later."
        try:
            return await self._propose_define(definition)
        except Exception:
            log.exception("proposing a collection failed")
            return "ERROR: could not ask the user to confirm; try again later."


async def apply_definition(session: AsyncSession, pricing: Pricing, d: Definition) -> Collection:
    """Create or extend the person's collection as proposed; the caller commits. All checks
    run again, so a proposal that went stale (limit reached, collection changed) fails."""
    bot = await session.get(Bot, d.bot_id)
    user = await session.get(User, d.user_id)
    if bot is None or user is None:
        raise CollectionNotFound(d.key)
    schema = _schema(d.schema)
    if d.collection_id is None:
        return await create_collection(
            session,
            pricing,
            bot=bot,
            key=d.key,
            title=d.title,
            scope=CollectionScope.PERSONAL,
            schema=schema,
            created_by=user,
        )
    space = await space_for(session, bot, user)
    c = await lock_collection(session, space, d.collection_id)
    if c.created_by_user_id != user.id:
        raise NotAllowed("only the person's own collections can be changed in chat")
    if c.schema_version != d.schema_version:
        raise InvalidSchema("the list changed since this was proposed; ask again")
    if (await get_collection(session, space, d.key)).id != c.id:
        raise InvalidSchema("this list is no longer available")
    await update_collection_schema(session, c, schema)
    return c
