"""A bot's collections as the factory designs them (SPEC 8.8, P5-4).

The designer writes CollectionDraft values; sync_collections turns them into the bot's
template collections through obot.data. It never deletes data: a collection missing from a
draft is left alone and a field missing from it is hidden.
"""

import logging
import re
from typing import Literal

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from obot.billing.plans import Pricing
from obot.data.errors import DataError, InvalidSchema, LimitReached
from obot.data.format import describe_error
from obot.data.repository import (
    check_shared_unique,
    collection_schema,
    create_collection,
    template_collections,
    update_collection_schema,
)
from obot.data.schema import KEY_PATTERN, CollectionSchema
from obot.db.models import Bot, Collection, CollectionScope

log = logging.getLogger(__name__)

FieldType = Literal[
    "text", "number", "money", "date", "time", "datetime", "choice", "boolean", "phone"
]


class DraftField(BaseModel):
    key: str = Field(description="Lowercase latin slug, e.g. 'date'.")
    label: str = Field(description="Shown to people, in the bot's language.")
    type: FieldType
    required: bool = False
    options: list[str] = Field(default=[], description="Choice fields only.")
    currency: str | None = Field(default=None, description="Money fields only, e.g. 'IRT'.")
    public: bool = Field(
        default=False,
        description=(
            "Shared collections only: everyone may see this field to know a slot is taken "
            "(e.g. date and time). Never names, phones or notes."
        ),
    )


class DraftWindow(BaseModel):
    weekdays: list[int] = Field(description="0 = Monday … 5 = Saturday, 6 = Sunday.")
    start: str = Field(description="HH:MM")
    end: str = Field(description="HH:MM, exclusive; use 23:59 for end of day")


class DraftHours(BaseModel):
    date_field: str
    time_field: str | None = Field(
        default=None, description="None when date_field is a datetime field."
    )
    windows: list[DraftWindow]


class CollectionDraft(BaseModel):
    key: str = Field(description="Lowercase latin slug, e.g. 'appointments'.")
    title: str = Field(description="Shown to people, in the bot's language.")
    scope: Literal["personal", "shared"] = Field(
        description=(
            "'personal': each user keeps their own (expenses, habits). 'shared': everyone "
            "adds and only the owner sees them all (bookings, orders, registrations)."
        )
    )
    fields: list[DraftField]
    unique: list[list[str]] = Field(
        default=[], description="Field groups that must not repeat, e.g. [['date','time']]."
    )
    allowed_hours: DraftHours | None = None


def _reasons(e: ValidationError, hidden: int = 0) -> str:
    out = []
    for err in e.errors():
        loc = ".".join(str(p) for p in err["loc"])
        msg = err["msg"].removeprefix("Value error, ")
        if err["loc"] == ("fields",) and err["type"] == "too_long":
            msg = f"too many fields, including {hidden} hidden ones"
        elif loc and not msg.startswith(loc.split(".")[-1]):
            msg = f"{loc}: {msg}"
        out.append(msg)
    return "; ".join(out)


def _schema_of(d: CollectionDraft, lang: str) -> CollectionSchema:
    """to_schema without the key prefix on its errors."""
    if not re.fullmatch(KEY_PATTERN, d.key):
        raise InvalidSchema("the key must be a lowercase latin slug")
    data = {
        "fields": [
            {**f.model_dump(exclude={"label"}, exclude_none=True), "label": {lang: f.label}}
            for f in d.fields
        ],
        "rules": {
            "unique": d.unique,
            "allowed_hours": d.allowed_hours.model_dump() if d.allowed_hours else None,
        },
    }
    try:
        schema = CollectionSchema.model_validate(data)
    except ValidationError as e:
        raise InvalidSchema(_reasons(e)) from None
    check_shared_unique(CollectionScope(d.scope), schema)
    return schema


def to_schema(d: CollectionDraft, lang: str) -> CollectionSchema:
    """The draft as a CollectionSchema; InvalidSchema names the collection's key."""
    try:
        return _schema_of(d, lang)
    except InvalidSchema as e:
        raise InvalidSchema(f"{d.key}: {e}") from None


def from_collection(c: Collection, lang: str) -> CollectionDraft:
    """A template collection as the designer sees it: visible fields, labels in `lang`."""

    def label(labels: dict[str, str], fallback: str) -> str:
        return labels.get(lang) or next(iter(labels.values()), None) or fallback

    schema = collection_schema(c)
    allowed = schema.rules.allowed_hours
    return CollectionDraft(
        key=c.key,
        title=label(c.title, c.key),
        scope=c.scope.value,
        fields=[
            DraftField(
                key=f.key,
                label=label(f.label, f.key),
                type=f.type.value,
                required=f.required,
                options=f.options,
                currency=f.currency,
                public=f.public,
            )
            for f in schema.fields
            if not f.hidden
        ],
        unique=schema.rules.unique,
        allowed_hours=DraftHours(
            date_field=allowed.date_field,
            time_field=allowed.time_field,
            windows=[
                DraftWindow(weekdays=w.weekdays, start=f"{w.start:%H:%M}", end=f"{w.end:%H:%M}")
                for w in allowed.windows
            ],
        )
        if allowed
        else None,
    )


def merge_schema(old: CollectionSchema, new: CollectionSchema) -> CollectionSchema:
    """`new` applied over `old`: old fields missing from `new` stay, hidden; labels merge by
    language so other languages' labels are kept. Existing fields keep their order and new ones
    are appended. min/max/currency the draft leaves unset are kept from the old field. Rules come
    from `new`."""
    new_fields = new.field_map()
    fields = []
    for f in old.fields:
        n = new_fields.get(f.key)
        if n is None:
            fields.append(f.model_copy(update={"hidden": True}))
        else:
            keep = {k: getattr(f, k) for k in ("min", "max", "currency") if getattr(n, k) is None}
            fields.append(n.model_copy(update={"label": {**f.label, **n.label}, **keep}))
    old_keys = old.field_map()
    fields += [n for n in new.fields if n.key not in old_keys]
    try:
        return CollectionSchema.model_validate(
            {
                "fields": [f.model_dump(mode="json") for f in fields],
                "rules": new.rules.model_dump(mode="json"),
            }
        )
    except ValidationError as e:
        raise InvalidSchema(_reasons(e, hidden=sum(f.hidden for f in fields))) from None


async def sync_collections(
    session: AsyncSession,
    pricing: Pricing,
    bot: Bot,
    drafts: list[CollectionDraft],
    lang: str,
) -> list[str]:
    """Create or extend the bot's template collections as drafted; the caller commits.

    Each collection is applied in its own savepoint, so one problem (a change that would
    break stored records, the plan's limit) never blocks the others. Returns the problems
    as "title: reason".
    """
    existing = {c.key: c for c in await template_collections(session, bot.id)}
    problems = []
    for d in drafts:
        try:
            async with session.begin_nested():
                schema = _schema_of(d, lang)
                c = existing.get(d.key)
                if c is None:
                    await create_collection(
                        session,
                        pricing,
                        bot=bot,
                        key=d.key,
                        title={lang: d.title},
                        scope=CollectionScope(d.scope),
                        schema=schema,
                    )
                    continue
                if c.scope.value != d.scope:
                    raise InvalidSchema(
                        "a list can't switch between personal and shared; make a new one"
                    )
                old = collection_schema(c)
                merged = merge_schema(old, schema)
                if merged != old:
                    await update_collection_schema(session, c, merged)
                if c.title.get(lang) != d.title:
                    c.title = {**c.title, lang: d.title}
                    await session.flush()
        except LimitReached as e:
            if e.limit == "collections":
                problems.append(f"{d.title}: your plan's limit of lists is reached")
            else:
                reason = describe_error(e).removeprefix("ERROR: ").rstrip(".")
                problems.append(f"{d.title}: {reason}")
        except DataError as e:
            reason = describe_error(e).removeprefix("ERROR: ").rstrip(".")
            problems.append(f"{d.title}: {reason}")
        except Exception:
            log.exception("syncing collection %r of bot %s failed", d.key, bot.id)
            problems.append(f"{d.title}: could not be saved, try again later")
    return problems
