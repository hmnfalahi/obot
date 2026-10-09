"""The data_* tools' access to one bot's collections on behalf of one person, in a private
context: their chat with the bot, their task, or another bot's question for them.

Each call opens its own session and commits writes at once, so no row locks or unique keys
are held while the model is thinking, and a booking is saved even if the reply fails later.
"""

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from obot.billing.plans import Pricing
from obot.data.errors import DataError, InvalidQuery, NotAllowed
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
    add_record,
    collection_schema,
    get_collection,
    get_record,
    list_collections,
    update_record,
)
from obot.data.space import DataSpace, can_change, space_for, tz_for
from obot.db.models import Bot, Collection, CollectionScope, Record, User

NoticeShared = Callable[[], Awaitable[None]]
ConfirmDelete = Callable[[Collection, Record], Awaitable[str]]

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
    ) -> None:
        self._sessionmaker = sessionmaker
        self._pricing = pricing
        self._bot_id = bot_id
        self._user_id = user_id
        self._notice_shared = notice_shared
        self._confirm_delete = confirm_delete

    async def _space(self, session: AsyncSession) -> DataSpace:
        bot = await session.get(Bot, self._bot_id)
        user = await session.get(User, self._user_id)
        return await space_for(session, bot, user)

    async def note(self, session: AsyncSession) -> str | None:
        """The catalog for the bot's instructions; None when it has no collections here."""
        space = await self._space(session)
        collections = await list_collections(session, space)
        if not collections:
            return None
        return describe_collections(collections, space, datetime.now(UTC))

    async def _run(self, key: str, action, *, write: bool = False) -> str:
        try:
            async with self._sessionmaker() as session:
                space = await self._space(session)
                c = await get_collection(session, space, key.strip())
                text = await action(session, space, c)
                if write:
                    await session.commit()
                return text
        except DataError as e:
            return describe_error(e)
        except DBAPIError as e:
            if _sqlstate(e) in RETRYABLE:
                return "ERROR: someone changed this at the same moment; try again."
            raise

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
            hidden = [f.key for f in collection_schema(c).fields if f.hidden]
            return format_records(records, tz_for(space, c), hidden=hidden)

        return await self._run(collection, action)

    async def add(self, collection: str, data: dict[str, Any]) -> str:
        async def action(session, space, c):
            record = await add_record(session, self._pricing, space, c, data)
            if (
                c.scope == CollectionScope.SHARED
                and not space.is_owner
                and self._notice_shared is not None
            ):
                await self._notice_shared()
            return f"OK: saved as #{record.id}: {json.dumps(record.data, ensure_ascii=False)}"

        return await self._run(collection, action, write=True)

    async def update(self, collection: str, record_id: int, changes: dict[str, Any]) -> str:
        async def action(session, space, c):
            record = await update_record(session, self._pricing, space, c, record_id, changes)
            return f"OK: #{record.id} is now {json.dumps(record.data, ensure_ascii=False)}"

        return await self._run(collection, action, write=True)

    async def delete(self, collection: str, record_id: int) -> str:
        if self._confirm_delete is None:
            return "ERROR: deleting needs the user's confirmation in their private chat."

        async def action(session, space, c):
            record = await get_record(session, space, c, record_id)
            if not can_change(space, c, record):
                raise NotAllowed("this user cannot delete this record")
            return await self._confirm_delete(c, record)

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
