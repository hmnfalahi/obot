"""A bot's data in the panel (SPEC 8.8): the same collections, rules and events as in chat.

Every call builds the caller's DataSpace for the bot and goes through obot.data; errors come
back as {code, message, errors, fields} so the app can explain them in the user's language.
"""

import json
import re
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Query, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import joinedload

from obot.api.data_export import build_csv, make_token, read_token
from obot.api.deps import CurrentUser, Session, State, UsableBot
from obot.bots.access import can_use
from obot.data.errors import (
    CollectionNotFound,
    DataError,
    DuplicateRecord,
    InvalidQuery,
    InvalidSchema,
    LimitReached,
    NotAllowed,
    RecordInvalid,
    RecordNotFound,
    SchemaChangeRejected,
    StaleRecord,
)
from obot.data.query import MAX_FIND, find_records
from obot.data.repository import (
    add_record,
    collection_by_id,
    collection_schema,
    delete_record,
    list_collections,
    lock_collection,
    set_collection_events,
    update_collection_schema,
    update_record,
)
from obot.data.schema import CollectionEvents, CollectionSchema
from obot.data.service import RETRYABLE, parse_filters
from obot.data.space import DataSpace, space_for, tz_for
from obot.db.models import Bot, BotStatus, Collection, Record, User
from obot.i18n import lang_of

router = APIRouter(prefix="/api")

PAGE_MAX = MAX_FIND - 1  # a page asks for one more record to tell whether more follow
MAX_SEARCH = 200
MAX_FILTERS = 10
CollectionId = Annotated[int, Path(ge=1, le=2**63 - 1)]
RecordId = Annotated[int, Path(ge=1, le=2**63 - 1)]


class CollectionOut(BaseModel):
    id: int
    key: str
    title: dict[str, str]
    scope: str
    mine: bool  # a personal collection this user made in chat
    is_owner: bool  # the caller owns the bot, so sees everyone's records of a shared one
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


_DATA_PATH = re.compile(r"^/api/(bots/[^/]+/collections(/|$)|exports/)")


def _is_data_path(request: Request) -> bool:
    return bool(_DATA_PATH.match(request.url.path))


async def validation_error(request: Request, exc: RequestValidationError) -> Response:
    """A malformed body or query on the data routes, in the same {code, ...} shape as
    every other data error; other routes keep FastAPI's default."""
    if not _is_data_path(request):
        return await request_validation_exception_handler(request, exc)
    errors = []
    for err in exc.errors():
        loc = [str(part) for part in err.get("loc", ())]
        if loc and loc[0] in ("body", "query", "path"):
            loc = loc[1:]
        errors.append({"field": ".".join(loc), "reason": str(err.get("msg", ""))})
    detail = {"code": "invalid", "message": "invalid request", "errors": errors, "fields": []}
    return JSONResponse({"detail": detail}, status_code=422)


async def database_error(request: Request, exc: DBAPIError) -> Response:
    """A deadlock or serialization failure on the data routes: try again (409 busy)."""
    sqlstate = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
    if not _is_data_path(request) or sqlstate not in RETRYABLE:
        raise exc
    detail = {"code": "busy", "message": "changed at the same moment", "errors": [], "fields": []}
    return JSONResponse({"detail": detail}, status_code=409)


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
    editor = can_edit(c, bot, user)
    schema = collection_schema(c).model_dump(mode="json")
    return CollectionOut(
        id=c.id,
        key=c.key,
        title=c.title,
        scope=c.scope.value,
        mine=c.created_by_user_id == user.id,
        is_owner=space.is_owner,
        can_edit=editor,
        schema_version=c.schema_version,
        fields=[f for f in schema["fields"] if editor or not f["hidden"]],
        rules=schema["rules"],
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
    offset: int = Query(default=0, ge=0, le=100_000),
    limit: int = Query(default=50, ge=1, le=PAGE_MAX),
) -> RecordsPage:
    space, c = await _space_and_collection(session, bot, user, collection_id)
    try:
        if q is not None and len(q) > MAX_SEARCH:
            raise InvalidQuery(f"search is at most {MAX_SEARCH} characters")
        raw = json.loads(filters) if filters else None
        parsed = parse_filters(raw)
        if len(parsed) > MAX_FILTERS:
            raise InvalidQuery(f"at most {MAX_FILTERS} filters")
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
        raise data_error(InvalidQuery("bad filters")) from None
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


class SchemaIn(BaseModel):
    fields: list[dict[str, Any]]
    rules: dict[str, Any] = {}
    title: dict[str, str] | None = None
    version: int


def _problem(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message, "errors": [], "fields": []})


def _clean_title(title: dict[str, str] | None) -> dict[str, str]:
    cleaned: dict[str, str] = {}
    for key, value in (title or {}).items():
        value = value.strip()
        if key not in ("fa", "en") or not 1 <= len(value) <= 100:
            raise InvalidSchema("a title is 1 to 100 characters, in fa or en")
        cleaned[key] = value
    return cleaned


async def _editable(session, bot: Bot, user: User, collection_id: int):
    space = await space_for(session, bot, user)
    try:
        c = await lock_collection(session, space, collection_id)
    except DataError as e:
        raise data_error(e) from None
    if not can_edit(c, bot, user):
        raise _problem(404, "collection_not_found", "not found")
    return space, c


@router.put("/bots/{bot_id}/collections/{collection_id}/schema")
async def put_schema(
    collection_id: CollectionId, body: SchemaIn, bot: UsableBot, user: CurrentUser, session: Session
) -> CollectionOut:
    """Replace fields and rules (additive once there are records); rules are replaced as
    sent, which is how a rule is removed on purpose."""
    space, c = await _editable(session, bot, user, collection_id)
    if c.schema_version != body.version:
        raise _problem(409, "stale", "the list changed meanwhile")
    try:
        title = _clean_title(body.title)
        try:
            schema = CollectionSchema.model_validate({"fields": body.fields, "rules": body.rules})
        except ValidationError as e:
            raise InvalidSchema("; ".join(err["msg"] for err in e.errors())) from None
        await update_collection_schema(session, c, schema)
    except DataError as e:
        raise data_error(e) from None
    if title:
        c.title = {**c.title, **title}
        await session.flush()
    return collection_out(c, bot, user, space)


@router.put("/bots/{bot_id}/collections/{collection_id}/events")
async def put_events(
    collection_id: CollectionId,
    body: CollectionEvents,
    bot: UsableBot,
    user: CurrentUser,
    session: Session,
) -> CollectionOut:
    space, c = await _editable(session, bot, user, collection_id)
    try:
        await set_collection_events(session, c, body)
    except DataError as e:
        raise data_error(e) from None
    return collection_out(c, bot, user, space)


class ExportOut(BaseModel):
    url: str
    file_name: str


@router.post("/bots/{bot_id}/collections/{collection_id}/export")
async def export_link(
    collection_id: CollectionId, bot: UsableBot, user: CurrentUser, session: Session, state: State
) -> ExportOut:
    _, c = await _space_and_collection(session, bot, user, collection_id)
    token = make_token(state.factory_token, bot.id, c.id, user.id, datetime.now(UTC))
    return ExportOut(url=f"/api/exports/{token}", file_name=f"{c.key}.csv")


@router.get("/exports/{token}")
async def export_csv(token: str, session: Session, state: State) -> Response:
    found = read_token(state.factory_token, token, datetime.now(UTC))
    if found is None:
        raise _problem(404, "export_link_invalid", "link expired")
    bot_id, collection_id, user_id = found
    bot = await session.get(Bot, bot_id, options=[joinedload(Bot.owner)])
    user = await session.get(User, user_id)
    if (
        bot is None
        or user is None
        or user.is_disabled
        or bot.status != BotStatus.ACTIVE
        or not can_use(bot, user.tg_user_id)
    ):
        raise _problem(404, "export_link_invalid", "link expired")
    space, c = await _space_and_collection(session, bot, user, collection_id)
    text = await build_csv(session, space, c, lang_of(user.language))
    return Response(
        content=text.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{c.key}.csv"'},
    )
