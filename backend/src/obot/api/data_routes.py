"""A bot's data in the panel (SPEC 8.8): the same collections, rules and events as in chat.

Every call builds the caller's DataSpace for the bot and goes through obot.data; errors come
back as {code, message, errors, fields} so the app can explain them in the user's language.
"""

import json
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel

from obot.api.deps import CurrentUser, Session, State, UsableBot
from obot.data.errors import (
    CollectionNotFound,
    DataError,
    DuplicateRecord,
    LimitReached,
    NotAllowed,
    RecordInvalid,
    RecordNotFound,
    SchemaChangeRejected,
    StaleRecord,
)
from obot.data.query import find_records
from obot.data.repository import (
    add_record,
    collection_by_id,
    collection_schema,
    delete_record,
    list_collections,
    update_record,
)
from obot.data.service import parse_filters
from obot.data.space import DataSpace, space_for, tz_for
from obot.db.models import Bot, Collection, Record, User

router = APIRouter(prefix="/api")

PAGE_MAX = 100
CollectionId = Annotated[int, Path(ge=1, le=2**63 - 1)]
RecordId = Annotated[int, Path(ge=1, le=2**63 - 1)]


class CollectionOut(BaseModel):
    id: int
    key: str
    title: dict[str, str]
    scope: str
    mine: bool  # a personal collection this user made in chat
    can_edit: bool
    schema_version: int
    fields: list[dict[str, Any]]
    rules: dict[str, Any]
    events: dict[str, Any]
    timezone: str  # the time zone dates and times of this collection are in


class RecordOut(BaseModel):
    id: int
    data: dict[str, Any]
    version: int
    created_at: datetime
    mine: bool


class RecordsPage(BaseModel):
    records: list[RecordOut]
    has_more: bool


class RecordIn(BaseModel):
    data: dict[str, Any]


class RecordPatch(BaseModel):
    changes: dict[str, Any]
    version: int | None = None


def data_error(e: DataError) -> HTTPException:
    status = 422
    if isinstance(e, CollectionNotFound | RecordNotFound):
        status = 404
    elif isinstance(e, NotAllowed):
        status = 403
    elif isinstance(e, StaleRecord):
        status = 409
    elif isinstance(e, LimitReached):
        status = 403
    detail: dict[str, Any] = {"code": e.code, "message": str(e), "errors": [], "fields": []}
    if isinstance(e, RecordInvalid):
        detail["errors"] = [{"field": f.field, "reason": f.reason} for f in e.errors]
    if isinstance(e, DuplicateRecord):
        detail["fields"] = e.fields
    if isinstance(e, LimitReached):
        detail["limit"] = e.limit
    if isinstance(e, SchemaChangeRejected):
        detail["change"] = getattr(e, "change", None)
        detail["fields"] = [e.field] if getattr(e, "field", None) else []
    return HTTPException(status, detail)


def can_edit(c: Collection, bot: Bot, user: User) -> bool:
    if c.created_by_user_id is None:
        return bot.owner_id == user.id
    return c.created_by_user_id == user.id


def visible_data(c: Collection, data: dict[str, Any]) -> dict[str, Any]:
    hidden = {f.key for f in collection_schema(c).fields if f.hidden}
    return {k: v for k, v in data.items() if k not in hidden}


def record_out(c: Collection, r: Record, user: User) -> RecordOut:
    return RecordOut(
        id=r.id,
        data=visible_data(c, r.data),
        version=r.version,
        created_at=r.created_at,
        mine=r.author_user_id == user.id,
    )


def collection_out(c: Collection, bot: Bot, user: User, space: DataSpace) -> CollectionOut:
    return CollectionOut(
        id=c.id,
        key=c.key,
        title=c.title,
        scope=c.scope.value,
        mine=c.created_by_user_id == user.id,
        can_edit=can_edit(c, bot, user),
        schema_version=c.schema_version,
        fields=c.schema["fields"],
        rules=c.rules,
        events=c.events or {},
        timezone=tz_for(space, c).key,
    )


async def _space_and_collection(
    session, bot: Bot, user: User, collection_id: int
) -> tuple[DataSpace, Collection]:
    space = await space_for(session, bot, user)
    try:
        return space, await collection_by_id(session, space, collection_id)
    except DataError as e:
        raise data_error(e) from None


@router.get("/bots/{bot_id}/collections")
async def list_bot_collections(
    bot: UsableBot, user: CurrentUser, session: Session
) -> list[CollectionOut]:
    space = await space_for(session, bot, user)
    return [collection_out(c, bot, user, space) for c in await list_collections(session, space)]


@router.get("/bots/{bot_id}/collections/{collection_id}/records")
async def list_records(
    collection_id: CollectionId,
    bot: UsableBot,
    user: CurrentUser,
    session: Session,
    q: str | None = None,
    sort: str | None = None,
    desc: bool = True,
    filters: str | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=PAGE_MAX),
) -> RecordsPage:
    space, c = await _space_and_collection(session, bot, user, collection_id)
    try:
        raw = json.loads(filters) if filters else None
        parsed = parse_filters(raw)
        rows = await find_records(
            session,
            space,
            c,
            parsed,
            sort=sort,
            descending=desc,
            limit=limit + 1,
            search=q,
            offset=offset,
        )
    except json.JSONDecodeError:
        raise HTTPException(422, {"code": "invalid_query", "message": "bad filters"}) from None
    except DataError as e:
        raise data_error(e) from None
    return RecordsPage(
        records=[record_out(c, r, user) for r in rows[:limit]], has_more=len(rows) > limit
    )


@router.post("/bots/{bot_id}/collections/{collection_id}/records", status_code=201)
async def create_record(
    collection_id: CollectionId,
    body: RecordIn,
    bot: UsableBot,
    user: CurrentUser,
    session: Session,
    state: State,
) -> RecordOut:
    space, c = await _space_and_collection(session, bot, user, collection_id)
    try:
        record = await add_record(session, state.pricing, space, c, body.data)
    except DataError as e:
        raise data_error(e) from None
    await session.refresh(record)
    return record_out(c, record, user)


@router.patch("/bots/{bot_id}/collections/{collection_id}/records/{record_id}")
async def change_record(
    collection_id: CollectionId,
    record_id: RecordId,
    body: RecordPatch,
    bot: UsableBot,
    user: CurrentUser,
    session: Session,
    state: State,
) -> RecordOut:
    space, c = await _space_and_collection(session, bot, user, collection_id)
    try:
        record = await update_record(
            session,
            state.pricing,
            space,
            c,
            record_id,
            body.changes,
            expected_version=body.version,
        )
    except DataError as e:
        raise data_error(e) from None
    await session.refresh(record)
    return record_out(c, record, user)


@router.delete("/bots/{bot_id}/collections/{collection_id}/records/{record_id}", status_code=204)
async def remove_record(
    collection_id: CollectionId,
    record_id: RecordId,
    bot: UsableBot,
    user: CurrentUser,
    session: Session,
) -> None:
    space, c = await _space_and_collection(session, bot, user, collection_id)
    try:
        await delete_record(session, space, c, record_id)
    except DataError as e:
        raise data_error(e) from None
