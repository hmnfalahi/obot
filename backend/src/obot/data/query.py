"""Reading records: structured filters, availability of shared slots and summaries.

Bots never write SQL: they pass Filter values, which are checked against the schema and
compiled here. Sums and counts run in the database, not in the model (SPEC 8.8).
"""

import enum
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import TIMESTAMP, ColumnElement, Date, Numeric, cast, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from obot.data.errors import InvalidQuery
from obot.data.repository import collection_schema
from obot.data.schema import NUMERIC_TYPES, CollectionSchema, FieldDef, FieldType, normalize_value
from obot.data.space import DataSpace, row_filter, tz_for
from obot.db.models import Collection, CollectionScope, Record

MAX_FIND = 100
MAX_GROUPS = 100


class FilterOp(enum.StrEnum):
    EQ = "eq"
    NE = "ne"
    LT = "lt"
    LTE = "lte"
    GT = "gt"
    GTE = "gte"
    CONTAINS = "contains"
    IN = "in"


@dataclass(frozen=True)
class Filter:
    field: str
    op: FilterOp
    value: Any


class Metric(enum.StrEnum):
    COUNT = "count"
    SUM = "sum"
    AVG = "avg"
    MIN = "min"
    MAX = "max"


class Period(enum.StrEnum):
    DAY = "day"
    WEEK = "week"  # weeks start on Monday
    MONTH = "month"


@dataclass(frozen=True)
class SummaryRow:
    group: str | None
    value: Decimal


def _field(schema: CollectionSchema, key: str) -> FieldDef:
    f = schema.field(key)
    if f is None or f.hidden:
        raise InvalidQuery(f"unknown field {key!r}")
    return f


def _typed(f: FieldDef) -> ColumnElement:
    """The field's value as a comparable SQL expression."""
    if f.type in NUMERIC_TYPES:
        return cast(Record.data[f.key].astext, Numeric)
    if f.type == FieldType.BOOLEAN:
        return Record.data[f.key].as_boolean()
    # ISO dates, times and datetimes compare correctly as text.
    return Record.data[f.key].astext


def _value(f: FieldDef, value: Any, tz: ZoneInfo) -> Any:
    try:
        stored = normalize_value(f, value, tz, check_limits=False)
    except (ValueError, TypeError) as e:
        raise InvalidQuery(f"{f.key}: {e}") from None
    return Decimal(stored) if f.type in NUMERIC_TYPES else stored


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _condition(schema: CollectionSchema, flt: Filter, tz: ZoneInfo) -> ColumnElement[bool]:
    f = _field(schema, flt.field)
    if flt.op == FilterOp.CONTAINS:
        if f.type not in (FieldType.TEXT, FieldType.PHONE):
            raise InvalidQuery(f"{f.key}: contains works on text fields only")
        needle = _escape_like(str(flt.value).strip())
        return Record.data[f.key].astext.ilike(f"%{needle}%", escape="\\")
    expr = _typed(f)
    if flt.op == FilterOp.IN:
        if not isinstance(flt.value, list | tuple) or not flt.value:
            raise InvalidQuery(f"{f.key}: 'in' needs a non-empty list")
        return expr.in_([_value(f, v, tz) for v in flt.value])
    value = _value(f, flt.value, tz)
    match flt.op:
        case FilterOp.EQ:
            return expr == value
        case FilterOp.NE:
            return expr != value
        case FilterOp.LT:
            return expr < value
        case FilterOp.LTE:
            return expr <= value
        case FilterOp.GT:
            return expr > value
        case FilterOp.GTE:
            return expr >= value
    raise InvalidQuery(f"unknown operator {flt.op!r}")


async def find_records(
    session: AsyncSession,
    space: DataSpace,
    c: Collection,
    filters: Sequence[Filter] = (),
    *,
    sort: str | None = None,
    descending: bool = True,
    limit: int = 20,
) -> list[Record]:
    """Records of `c` the space may read, newest first unless sorted by a field."""
    schema = collection_schema(c)
    tz = tz_for(space, c)
    query = select(Record).where(
        *row_filter(space, c), *[_condition(schema, f, tz) for f in filters]
    )
    if sort is not None:
        key = _typed(_field(schema, sort))
        query = query.order_by(
            key.desc().nulls_last() if descending else key.asc().nulls_last(), Record.id
        )
    else:
        query = query.order_by(Record.id.desc() if descending else Record.id)
    rows = await session.scalars(query.limit(max(1, min(limit, MAX_FIND))))
    return list(rows)


async def availability(
    session: AsyncSession,
    space: DataSpace,
    c: Collection,
    filters: Sequence[Filter] = (),
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """The public fields of matching records of a shared collection, whoever wrote them:
    enough to tell that a slot is taken, never by whom (SPEC 8.8)."""
    if c.scope != CollectionScope.SHARED:
        raise InvalidQuery("availability works on shared collections only")
    schema = collection_schema(c)
    public = [f.key for f in schema.fields if f.public and not f.hidden]
    if not public:
        raise InvalidQuery("this collection has no public fields")
    if any(f.field not in public for f in filters):
        raise InvalidQuery("availability can filter on public fields only")
    tz = tz_for(space, c)
    rows = await session.scalars(
        select(Record.data)
        .where(
            Record.collection_id == c.id,
            Record.deleted_at.is_(None),
            *[_condition(schema, f, tz) for f in filters],
        )
        .order_by(Record.id)
        .limit(max(1, min(limit, MAX_FIND)))
    )
    return [{k: data[k] for k in public if k in data} for data in rows]


def _period_expr(schema: CollectionSchema, period: Period, field: str | None, tz: ZoneInfo):
    if field is None:
        dated = [
            f
            for f in schema.fields
            if f.type in (FieldType.DATE, FieldType.DATETIME) and not f.hidden
        ]
        if not dated:
            raise InvalidQuery("grouping by period needs a date field")
        f = dated[0]
    else:
        f = _field(schema, field)
        if f.type not in (FieldType.DATE, FieldType.DATETIME):
            raise InvalidQuery(f"{f.key} is not a date field")
    if f.type == FieldType.DATE:
        day = cast(Record.data[f.key].astext, Date)
    else:
        moment = cast(Record.data[f.key].astext, TIMESTAMP(timezone=True))
        day = func.timezone(tz.key, moment)
    fmt = "YYYY-MM" if period == Period.MONTH else "YYYY-MM-DD"
    return func.to_char(func.date_trunc(period.value, day), fmt)


async def summarize(
    session: AsyncSession,
    space: DataSpace,
    c: Collection,
    metric: Metric,
    field: str | None = None,
    *,
    filters: Sequence[Filter] = (),
    group_by: str | None = None,
    period: Period | None = None,
    period_field: str | None = None,
) -> list[SummaryRow]:
    """count/sum/avg/min/max over the records the space may read, optionally grouped by a
    field or by day/week/month of a date field (the first one unless `period_field`)."""
    schema = collection_schema(c)
    tz = tz_for(space, c)
    if metric == Metric.COUNT:
        value = func.count(Record.id)
    else:
        if field is None:
            raise InvalidQuery(f"{metric} needs a field")
        f = _field(schema, field)
        if f.type not in NUMERIC_TYPES:
            raise InvalidQuery(f"{f.key} is not a number")
        value = getattr(func, metric.value)(_typed(f))
    if group_by is not None and period is not None:
        raise InvalidQuery("group by a field or by a period, not both")
    if group_by is not None:
        group = Record.data[_field(schema, group_by).key].astext
    elif period is not None:
        group = _period_expr(schema, period, period_field, tz)
    else:
        group = None

    where = [*row_filter(space, c), *[_condition(schema, f, tz) for f in filters]]
    if group is None:
        result = await session.scalar(select(value).where(*where))
        return [SummaryRow(None, Decimal(result or 0))]
    # Group and order by the output alias: repeating the expression would repeat its bind
    # parameters, which Postgres does not treat as the same expression.
    rows = await session.execute(
        select(group.label("g"), value.label("v"))
        .where(*where)
        .group_by(text("g"))
        .order_by(text("g"))
        .limit(MAX_GROUPS)
    )
    return [SummaryRow(g, Decimal(v or 0)) for g, v in rows]
