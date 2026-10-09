"""A bot's collections as the factory designs them (SPEC 8.8, P5-4).

The designer writes CollectionDraft values; sync_collections turns them into the bot's
template collections through obot.data. It never deletes data: a collection missing from a
draft is left alone and a field missing from it is hidden.
"""

import logging
import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from obot.billing.plans import Pricing
from obot.data.describe import label_of
from obot.data.errors import DataError, InvalidSchema, LimitReached, SchemaChangeRejected
from obot.data.events import collection_events
from obot.data.format import describe_error
from obot.data.repository import (
    check_shared_unique,
    collection_schema,
    create_collection,
    set_collection_events,
    template_collections,
    update_collection_schema,
)
from obot.data.schema import (
    KEY_PATTERN,
    AllowedHours,
    CollectionEvents,
    CollectionSchema,
    FieldDef,
    NotifyRule,
    RemindRule,
    Rules,
)
from obot.data.schema import FieldType as SchemaFieldType
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


class DraftReminder(BaseModel):
    date_field: str
    time_field: str | None = Field(default=None, description="With a date field.")
    before_minutes: int = Field(
        ge=0, le=30 * 24 * 60, description="e.g. 1440 for a day before, 60 for an hour."
    )


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
    notify_owner: bool | None = Field(
        default=None,
        description="Shared only: message the owner about each new record. Omit to keep.",
    )
    notify_author_on_change: bool | None = Field(
        default=None,
        description=(
            "Shared only: tell the person when someone else changes or removes their "
            "record. Omit to keep."
        ),
    )
    reminder: DraftReminder | None = Field(
        default=None, description="Remind the person before their record's date. Omit to keep."
    )
    no_reminder: bool = Field(default=False, description="True removes the reminder.")


def to_events(d: CollectionDraft, current: CollectionEvents) -> CollectionEvents:
    """The draft's switches over the current events; omitted switches keep their value."""
    notify = {(n.on, n.to) for n in current.notify}
    if d.notify_owner is not None:
        notify.discard(("create", "owner"))
        if d.notify_owner:
            notify.add(("create", "owner"))
    if d.notify_author_on_change is not None:
        notify -= {("update", "author"), ("delete", "author")}
        if d.notify_author_on_change:
            notify |= {("update", "author"), ("delete", "author")}
    remind = current.remind
    if d.no_reminder:
        remind = []
    elif d.reminder is not None and not _same_reminder(d.reminder, current.remind):
        try:
            remind = [RemindRule(**d.reminder.model_dump())]
        except ValidationError as e:
            raise InvalidSchema(f"{d.key}: reminder: {_reasons(e)}") from None
    order = ["create", "update", "delete"]
    return CollectionEvents(
        notify=[
            NotifyRule(on=on, to=to)
            for on, to in sorted(notify, key=lambda n: (order.index(n[0]), n[1]))
        ],
        remind=remind,
    )


def _same_reminder(r: DraftReminder, current: list[RemindRule]) -> bool:
    """The designer echoing the current first reminder changes nothing."""
    return bool(current) and (
        (current[0].date_field, current[0].time_field, current[0].before_minutes)
        == (r.date_field, r.time_field, r.before_minutes)
    )


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
    schema = collection_schema(c)
    allowed = schema.rules.allowed_hours
    events = collection_events(c)
    pairs = {(n.on, n.to) for n in events.notify}
    reminder = events.remind[0] if events.remind else None
    return CollectionDraft(
        key=c.key,
        title=label_of(c.title, lang, c.key),
        scope=c.scope.value,
        fields=[
            DraftField(
                key=f.key,
                label=label_of(f.label, lang, f.key),
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
        notify_owner=("create", "owner") in pairs,
        notify_author_on_change=("update", "author") in pairs or ("delete", "author") in pairs,
        reminder=DraftReminder(
            date_field=reminder.date_field,
            time_field=reminder.time_field,
            before_minutes=reminder.before_minutes,
        )
        if reminder
        else None,
    )


class TooManyFields(InvalidSchema):
    """The merged schema has more fields than allowed; `hidden` of them are hidden ones."""

    def __init__(self, message: str, hidden: int) -> None:
        super().__init__(message)
        self.hidden = hidden


def _hours_fit(allowed: AllowedHours, fields: dict[str, FieldDef]) -> bool:
    day = fields.get(allowed.date_field)
    if allowed.time_field is None:
        return day is not None and day.type == SchemaFieldType.DATETIME
    at = fields.get(allowed.time_field)
    return (
        day is not None
        and day.type == SchemaFieldType.DATE
        and at is not None
        and at.type == SchemaFieldType.TIME
    )


def _merge_rules(old: Rules, new: Rules, fields: dict[str, FieldDef]) -> Rules:
    """The factory only adds rules: every old unique group stays, in its old order (a reordered
    group is the same rule), and the old allowed hours stay unless the draft sets new ones (or
    retypes their fields, which only an empty collection allows). Removing a rule is for the
    owner's panel."""
    known = {frozenset(g) for g in old.unique}
    unique = old.unique + [g for g in new.unique if frozenset(g) not in known]
    allowed = new.allowed_hours
    if allowed is None and old.allowed_hours and _hours_fit(old.allowed_hours, fields):
        allowed = old.allowed_hours
    return Rules(unique=unique, allowed_hours=allowed)


def merge_schema(old: CollectionSchema, new: CollectionSchema) -> CollectionSchema:
    """`new` applied over `old`: old fields missing from `new` stay, hidden; labels merge by
    language so other languages' labels are kept. Existing fields keep their order and new ones
    are appended. min/max/currency the draft leaves unset are kept from the old field. Rules
    merge as in _merge_rules."""
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
    hidden = sum(f.hidden for f in fields)
    by_key = {f.key: f for f in fields}
    try:
        return CollectionSchema.model_validate(
            {
                "fields": [f.model_dump(mode="json") for f in fields],
                "rules": _merge_rules(old.rules, new.rules, by_key).model_dump(mode="json"),
            }
        )
    except ValidationError as e:
        if any(err["loc"] == ("fields",) and err["type"] == "too_long" for err in e.errors()):
            raise TooManyFields(_reasons(e, hidden=hidden), hidden) from None
        raise InvalidSchema(_reasons(e, hidden=hidden)) from None


@dataclass(frozen=True)
class Problem:
    """A collection that could not be applied, for the UI to word for people. `reason`:
    limit | plan_limit | kind_change | breaks_records | too_many_fields | invalid | failed.
    breaks_records carries `change` (SchemaChangeRejected.change) and `field`, the field's
    label in the bot's language; too_many_fields carries `hidden`. Engine wording stays in
    the logs."""

    title: str
    reason: str
    change: str | None = None
    field: str | None = None
    hidden: int = 0


def _field_label(schemas: list[CollectionSchema], key: str | None, lang: str) -> str | None:
    if key is None:
        return None
    for schema in schemas:
        if (f := schema.field(key)) is not None:
            return label_of(f.label, lang, key)
    return key


def _log_problem(bot: Bot, d: CollectionDraft, e: DataError) -> None:
    log.info("collection %r of bot %s not applied: %s", d.key, bot.id, describe_error(e))


async def _sync_events(session: AsyncSession, c: Collection, d: CollectionDraft) -> None:
    current = collection_events(c)
    events = to_events(d, current)
    if events != current:
        await set_collection_events(session, c, events)


async def sync_collections(
    session: AsyncSession,
    pricing: Pricing,
    bot: Bot,
    drafts: list[CollectionDraft],
    lang: str,
) -> list[Problem]:
    """Create or extend the bot's template collections as drafted; the caller commits.

    Each collection is applied in its own savepoint, so one problem (a change that would
    break stored records, the plan's limit) never blocks the others. Returns the problems
    as Problem values.
    """
    existing = {c.key: c for c in await template_collections(session, bot.id)}
    problems = []
    for d in drafts:
        schemas: list[CollectionSchema] = []
        try:
            async with session.begin_nested():
                schema = _schema_of(d, lang)
                c = existing.get(d.key)
                if c is None:
                    c = await create_collection(
                        session,
                        pricing,
                        bot=bot,
                        key=d.key,
                        title={lang: d.title},
                        scope=CollectionScope(d.scope),
                        schema=schema,
                    )
                    await _sync_events(session, c, d)
                    continue
                if c.scope.value != d.scope:
                    problems.append(Problem(d.title, "kind_change"))
                    continue
                old = collection_schema(c)
                schemas = [schema, old]
                merged = merge_schema(old, schema)
                if merged != old:
                    await update_collection_schema(session, c, merged)
                if c.title.get(lang) != d.title:
                    c.title = {**c.title, lang: d.title}
                    await session.flush()
                await _sync_events(session, c, d)
        except LimitReached as e:
            _log_problem(bot, d, e)
            problems.append(Problem(d.title, "limit" if e.limit == "collections" else "plan_limit"))
        except SchemaChangeRejected as e:
            _log_problem(bot, d, e)
            label = _field_label(schemas, e.field, lang)
            problems.append(Problem(d.title, "breaks_records", change=e.change, field=label))
        except TooManyFields as e:
            _log_problem(bot, d, e)
            problems.append(Problem(d.title, "too_many_fields", hidden=e.hidden))
        except DataError as e:
            _log_problem(bot, d, e)
            problems.append(Problem(d.title, "invalid"))
        except Exception:
            log.exception("syncing collection %r of bot %s failed", d.key, bot.id)
            problems.append(Problem(d.title, "failed"))
    return problems
