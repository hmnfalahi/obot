"""Text the model reads about a bot's data: the collection catalog for its instructions,
records, summaries and errors (SPEC 8.8)."""

import json
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from obot.data.errors import (
    CollectionNotFound,
    DataError,
    DuplicateRecord,
    LimitReached,
    OutsideAllowedHours,
    RecordInvalid,
    RecordNotFound,
    StaleRecord,
)
from obot.data.query import Metric, SummaryRow
from obot.data.repository import collection_schema
from obot.data.schema import AllowedHours, FieldDef, FieldType
from obot.data.space import DataSpace
from obot.db.models import Collection, CollectionScope, Record

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

DATA_RULES = (
    "Dates are Gregorian YYYY-MM-DD (convert Persian/Jalali dates first), times HH:MM "
    "(24h), datetimes YYYY-MM-DDTHH:MM local time of the collection. Save things the user asks "
    "you to keep with data_add, and never say something is saved unless data_add returned "
    "OK. Look records up with data_find and compute totals with data_summarize; never add "
    "numbers up yourself. Deleting shows the user a confirm button."
)


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False)


def _number(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def describe_allowed(allowed: AllowedHours) -> str:
    windows = "; ".join(
        f"{','.join(WEEKDAYS[d] for d in w.weekdays)} {w.start:%H:%M}-{w.end:%H:%M}"
        for w in allowed.windows
    )
    return f"{windows} (end excluded)"


def _describe_field(f: FieldDef) -> str:
    kind = f"money {f.currency}" if f.type == FieldType.MONEY and f.currency else f.type.value
    if f.options:
        kind += ": " + "|".join(f.options)
    parts = [kind]
    if f.min is not None:
        parts.append(f"min {_number(f.min)}")
    if f.max is not None:
        parts.append(f"max {_number(f.max)}")
    if f.required:
        parts.append("required")
    labels = " / ".join(dict.fromkeys(f.label.values()))
    return f'{f.key} ({", ".join(parts)}) "{labels}"'


def _describe_scope(c: Collection, space: DataSpace) -> str:
    match c.scope:
        case CollectionScope.PERSONAL:
            return "personal: each user has their own records"
        case CollectionScope.SHARED if space.is_owner:
            return (
                "shared: everyone who uses the bot adds records; this user owns this bot "
                "and sees everyone's records"
            )
        case CollectionScope.SHARED:
            return (
                "shared: this user sees only this user's own records; to see what anyone has "
                "taken, call data_find with availability=true (public fields only)"
            )
    return "group: shared by the members of this group"


def _zone(c: Collection, space: DataSpace) -> str:
    """The time zone a collection's dates and times are written in (see space.tz_for)."""
    return space.timezone if c.scope == CollectionScope.PERSONAL else space.owner_timezone


def _moment(t: datetime) -> str:
    return f"{t:%A %Y-%m-%d}, {t:%H:%M}"


def describe_collections(collections: list[Collection], space: DataSpace, now: datetime) -> str:
    """The catalog that goes into the bot's instructions."""
    lines = ["Your data collections (use the data_* tools with these exact keys):"]
    for c in collections:
        schema = collection_schema(c)
        title = " / ".join(dict.fromkeys(c.title.values())) or c.key
        fields = "; ".join(_describe_field(f) for f in schema.fields if not f.hidden)
        line = f'- {c.key} "{title}" ({_describe_scope(c, space)}). Fields: {fields}.'
        for group in schema.rules.unique:
            line += f" Rule: at most one record per {'+'.join(group)}."
        if (allowed := schema.rules.allowed_hours) is not None:
            line += f" Allowed times: {describe_allowed(allowed)}."
        line += f" Times are in {_zone(c, space)}."
        public = [f.key for f in schema.fields if f.public and not f.hidden]
        if c.scope == CollectionScope.SHARED and public:
            line += f" public fields: {', '.join(public)}."
        lines.append(line)
    local = now.astimezone(ZoneInfo(space.timezone))
    lines.append(f"{DATA_RULES} Today is {_moment(local)} in {space.timezone}.")
    if space.owner_timezone != space.timezone and any(
        c.scope != CollectionScope.PERSONAL for c in collections
    ):
        owner = now.astimezone(ZoneInfo(space.owner_timezone))
        lines.append(f"In {space.owner_timezone} it is {_moment(owner)}.")
    return "\n".join(lines)


def format_records(records: list[Record], tz: ZoneInfo, hidden: Iterable[str] = ()) -> str:
    """One line per record; `hidden` field keys are left out of the values."""
    if not records:
        return "No records."
    drop = set(hidden)
    return "\n".join(
        f"#{r.id} ({r.created_at.astimezone(tz):%Y-%m-%d}) "
        f"{_json({k: v for k, v in r.data.items() if k not in drop})}"
        for r in records
    )


def format_taken(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "Nothing taken matches."
    return "\n".join(f"Taken: {_json(row)}" for row in rows)


def format_summary(rows: list[SummaryRow], metric: Metric) -> str:
    def value(v: Decimal) -> str:
        return _number(v.quantize(Decimal("0.01")) if metric == Metric.AVG else v)

    if len(rows) == 1 and rows[0].group is None:
        return f"{metric.value}: {value(rows[0].value)}"
    lines = [f"{metric.value} by group:"]
    lines += [f"{r.group if r.group is not None else '(empty)'}: {value(r.value)}" for r in rows]
    return "\n".join(lines)


def describe_error(e: DataError) -> str:
    match e:
        case RecordInvalid():
            text = "invalid values: " + "; ".join(f"{f.field}: {f.reason}" for f in e.errors)
        case DuplicateRecord():
            text = f"the same {'+'.join(e.fields)} already exists (taken); suggest another"
        case OutsideAllowedHours():
            text = f"outside the allowed times: {describe_allowed(e.allowed)}"
        case LimitReached() if e.limit == "records":
            text = "the record limit of the plan is reached"
        case LimitReached() if e.limit == "record_bytes":
            text = "the record is too large"
        case StaleRecord():
            text = "the record changed meanwhile; look it up again with data_find"
        case CollectionNotFound():
            text = f"there is no collection named {e.key!r}"
        case RecordNotFound():
            text = f"there is no record #{e.record_id} this user can see"
        case _:
            text = str(e).removesuffix(".")
    return f"ERROR: {text}."
