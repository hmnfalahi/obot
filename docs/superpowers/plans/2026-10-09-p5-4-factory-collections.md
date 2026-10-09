# P5-4: The Factory Designs a Bot's Data Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When an owner tells the factory "I'm a dentist, patients should book visits", the designer guesses the bot's collections (fields, rules), the draft card shows them in plain words in a "Data" section, and Create/Save turns them into the bot's template collections. Talking to the factory again ("also Thursdays 9–12") changes them.

**Architecture:**
- **Rendering for people:** a new module `obot/data/describe.py` renders collections in plain fa/en for people. It takes over the type names that P5-3 put in `telegram/data_chat.py`.
- **Draft models and sync:** a new module `obot/factory/collections.py` holds three things:
  - the designer-facing models `CollectionDraft` / `DraftField` / `DraftHours`;
  - their conversion to `CollectionSchema`;
  - `sync_collections`, which creates or extends the bot's template collections through `obot.data`, one savepoint per collection, and returns readable problems instead of failing the whole save.
- **Designer:** `BotDraft` gains `collections`, and a pydantic-ai output validator asks the model to retry when a collection is invalid.
- **Wiring:** `apply_draft` runs the sync, and the factory card shows the Data section.

**Tech Stack:** Python 3.13, pydantic 2, pydantic-ai (output validators, `ModelRetry`), SQLAlchemy 2 async, aiogram 3.

**Spec:** `docs/SPEC.md` section ۸٫۸, row P5-4: "کارخانه: حدس طرح، قواعد و رویدادها در designer؛ بخش «داده‌ها» در کارت پیش‌نویس". Builds on P5-1..P5-3 on branch `p5-data-core`.

## Global Constraints

- **Events are out of scope (P5-5).** The designer never promises notifications or reminders yet. Its instructions say so explicitly, and the draft has no events.
- **Scopes:** the designer may only produce PERSONAL and SHARED collections; GROUP is P5-7.
- **Never delete data from the factory.** A collection that drops out of the draft is left alone. A field that drops out is hidden, not removed (`check_evolution` would refuse removal anyway).
- **Fixed scope:** the scope of an existing collection never changes.
- **Additive changes only:** schema changes to collections that have records are decided by `obot.data` (`check_evolution`, `check_shared_unique`).
- **Owner's plan:** template collections count against the owner's plan (`create_collection` already does this).
- **Shared collections:** fields named by a unique rule must be `public`. Names and phone numbers are never public.
- **Readable problems:** a problem with one collection never blocks the bot's other changes. The owner gets a readable list of what was not applied.
- **Hand-written i18n:** user-facing texts live in `obot/i18n.py` (fa/en), with no machine-translated phrasing.
- **Commits:** one plain English imperative sentence, ending with a `Co-Authored-By:` trailer. Never commit the user's untracked `backend/avatars.jsonl` or `backend/scripts/`, and don't run `ruff --fix` outside `src/` and `tests/`.
- **Running tests:** dev infra must be up (`docker compose up -d`). Run tests from `backend/`.

## File Structure

| File | Responsibility |
|---|---|
| `backend/src/obot/data/describe.py` (create) | Collections in plain words for people: field, rules, collection summary |
| `backend/src/obot/telegram/data_chat.py` (modify) | Use `describe` instead of its own `TYPE_NAMES` |
| `backend/src/obot/data/repository.py` (modify) | `check_shared_unique` made public; `template_collections` |
| `backend/src/obot/factory/collections.py` (create) | `DraftField`, `DraftWindow`, `DraftHours`, `CollectionDraft`, `to_schema`, `from_collection`, `merge_schema`, `sync_collections` |
| `backend/src/obot/factory/designer.py` (modify) | `BotDraft.collections`, instructions, `check_output` + output validator |
| `backend/src/obot/factory/drafts.py` (modify) | `apply_draft(..., pricing) -> list[str]`; existing bots' drafts carry their collections |
| `backend/src/obot/telegram/factory.py` (modify) | Data section on the card; report problems after Create/Save |
| `backend/src/obot/i18n.py` (modify) | Type names, weekdays, scope and rule phrases, card/problem texts |
| Tests: `test_data_describe.py`, `test_factory_collections.py` (create); `test_factory.py`, `test_data_chat.py` (modify) | |

---

### Task 1: Collections in plain words

**Files:**
- Create: `backend/src/obot/data/describe.py`
- Modify: `backend/src/obot/i18n.py`, `backend/src/obot/telegram/data_chat.py` (`TYPE_NAMES`, `definition_text`)
- Test: `backend/tests/test_data_describe.py`

**Interfaces:**
- Produces: `label_of(labels: dict[str, str], lang: str, fallback: str) -> str`; `field_text(f: FieldDef, lang: str) -> str` (e.g. `"تاریخ (تاریخ)"`, `"دسته (انتخابی: غذا، اجاره)"`); `rules_text(schema: CollectionSchema, lang: str) -> list[str]`; `collection_text(title: str, scope: CollectionScope, schema: CollectionSchema, lang: str) -> str`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_data_describe.py`:

```python
from obot.data.describe import collection_text, field_text, label_of, rules_text
from obot.data.schema import CollectionSchema
from obot.db.models import CollectionScope

BOOKINGS = CollectionSchema.model_validate(
    {
        "fields": [
            {"key": "name", "label": {"fa": "نام", "en": "Name"}, "type": "text",
             "required": True},
            {"key": "date", "label": {"fa": "تاریخ", "en": "Date"}, "type": "date",
             "public": True},
            {"key": "time", "label": {"fa": "ساعت", "en": "Time"}, "type": "time",
             "public": True},
            {"key": "reason", "label": {"fa": "علت"}, "type": "choice",
             "options": ["چکاپ", "درد"]},
            {"key": "old", "label": {"en": "Old"}, "type": "text", "hidden": True},
        ],
        "rules": {
            "unique": [["date", "time"]],
            "allowed_hours": {
                "date_field": "date",
                "time_field": "time",
                "windows": [
                    {"weekdays": [5, 6, 0, 1, 2], "start": "16:00", "end": "20:00"},
                    {"weekdays": [3], "start": "09:00", "end": "12:00"},
                ],
            },
        },
    }
)


def test_labels_fall_back_to_any_language_then_the_key() -> None:
    assert label_of({"fa": "نام", "en": "Name"}, "en", "name") == "Name"
    assert label_of({"fa": "علت"}, "en", "reason") == "علت"
    assert label_of({}, "fa", "reason") == "reason"


def test_field_text() -> None:
    f = BOOKINGS.field_map()
    assert field_text(f["date"], "fa") == "تاریخ (تاریخ)"
    assert field_text(f["reason"], "fa") == "علت (انتخابی: چکاپ، درد)"
    assert field_text(f["reason"], "en") == "علت (choice: چکاپ, درد)"


def test_rules_in_plain_words() -> None:
    assert rules_text(BOOKINGS, "fa") == [
        "هر تاریخ + ساعت فقط یک بار",
        "شنبه، یکشنبه، دوشنبه، سه‌شنبه، چهارشنبه ۱۶:۰۰ تا ۲۰:۰۰؛ پنجشنبه ۰۹:۰۰ تا ۱۲:۰۰",
    ]
    assert rules_text(BOOKINGS, "en") == [
        "one per Date + Time",
        "Mon, Tue, Wed, Sat, Sun 16:00–20:00; Thu 09:00–12:00",
    ]


def test_collection_summary() -> None:
    text = collection_text("نوبت‌ها", CollectionScope.SHARED, BOOKINGS, "fa")
    assert text.splitlines()[0] == "• نوبت‌ها — همه ثبت می‌کنند، فقط شما همه را می‌بینید"
    assert "نام (متن)" in text and "Old" not in text
    assert "هر تاریخ + ساعت فقط یک بار" in text
    personal = collection_text("Expenses", CollectionScope.PERSONAL, BOOKINGS, "en")
    assert personal.splitlines()[0] == "• Expenses — each user keeps their own"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && uv run pytest tests/test_data_describe.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'obot.data.describe'`.

- [ ] **Step 3: Add i18n keys**

In `backend/src/obot/i18n.py`, `"en"`:

```python
        "ftype.text": "text",
        "ftype.number": "number",
        "ftype.money": "amount",
        "ftype.date": "date",
        "ftype.time": "time",
        "ftype.datetime": "date and time",
        "ftype.choice": "choice",
        "ftype.boolean": "yes/no",
        "ftype.phone": "phone",
        "weekday.0": "Mon",
        "weekday.1": "Tue",
        "weekday.2": "Wed",
        "weekday.3": "Thu",
        "weekday.4": "Fri",
        "weekday.5": "Sat",
        "weekday.6": "Sun",
        "data.scope.personal": "each user keeps their own",
        "data.scope.shared": "everyone adds, only you see them all",
        "data.rule.unique": "one per {fields}",
        "data.rule.window": "{days} {start}–{end}",
```

`"fa"`:

```python
        "ftype.text": "متن",
        "ftype.number": "عدد",
        "ftype.money": "مبلغ",
        "ftype.date": "تاریخ",
        "ftype.time": "ساعت",
        "ftype.datetime": "تاریخ و ساعت",
        "ftype.choice": "انتخابی",
        "ftype.boolean": "بله/خیر",
        "ftype.phone": "تلفن",
        "weekday.0": "دوشنبه",
        "weekday.1": "سه‌شنبه",
        "weekday.2": "چهارشنبه",
        "weekday.3": "پنجشنبه",
        "weekday.4": "جمعه",
        "weekday.5": "شنبه",
        "weekday.6": "یکشنبه",
        "data.scope.personal": "هر کاربر برای خودش",
        "data.scope.shared": "همه ثبت می‌کنند، فقط شما همه را می‌بینید",
        "data.rule.unique": "هر {fields} فقط یک بار",
        "data.rule.window": "{days} {start} تا {end}",
```

- [ ] **Step 4: Write the module**

Create `backend/src/obot/data/describe.py`:

```python
"""Collections in plain words for people: the factory's draft card and the chat cards.

The model reads obot.data.format instead; this module is for owners and users, in fa/en.
"""

from obot.data.schema import AllowedHours, CollectionSchema, FieldDef
from obot.db.models import CollectionScope
from obot.i18n import t

_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
# Persian weeks start on Saturday; days are listed in the week order of the language.
_WEEK_ORDER = {"fa": [5, 6, 0, 1, 2, 3, 4], "en": [0, 1, 2, 3, 4, 5, 6]}


def label_of(labels: dict[str, str], lang: str, fallback: str) -> str:
    return labels.get(lang) or next(iter(labels.values()), None) or fallback


def _separator(lang: str) -> str:
    return "، " if lang == "fa" else ", "


def _clock(value, lang: str) -> str:
    text = f"{value:%H:%M}"
    return text.translate(_FA_DIGITS) if lang == "fa" else text


def field_text(f: FieldDef, lang: str) -> str:
    kind = t(lang, f"ftype.{f.type.value}")
    if f.options:
        kind += ": " + _separator(lang).join(f.options)
    return f"{label_of(f.label, lang, f.key)} ({kind})"


def _windows_text(allowed: AllowedHours, lang: str) -> str:
    order = _WEEK_ORDER.get(lang, _WEEK_ORDER["en"])
    parts = []
    for w in allowed.windows:
        days = _separator(lang).join(
            t(lang, f"weekday.{d}") for d in sorted(w.weekdays, key=order.index)
        )
        parts.append(
            t(
                lang,
                "data.rule.window",
                days=days,
                start=_clock(w.start, lang),
                end=_clock(w.end, lang),
            )
        )
    return "؛ ".join(parts) if lang == "fa" else "; ".join(parts)


def rules_text(schema: CollectionSchema, lang: str) -> list[str]:
    fields = schema.field_map()
    lines = [
        t(
            lang,
            "data.rule.unique",
            fields=" + ".join(label_of(fields[k].label, lang, k) for k in group),
        )
        for group in schema.rules.unique
    ]
    if schema.rules.allowed_hours is not None:
        lines.append(_windows_text(schema.rules.allowed_hours, lang))
    return lines


def collection_text(
    title: str, scope: CollectionScope, schema: CollectionSchema, lang: str
) -> str:
    """A bullet with the title and who sees what, then its visible fields and rules."""
    lines = [f"• {title} — {t(lang, f'data.scope.{scope.value}')}"]
    visible = [field_text(f, lang) for f in schema.fields if not f.hidden]
    lines.append("   " + _separator(lang).join(visible))
    lines += [f"   {rule}" for rule in rules_text(schema, lang)]
    return "\n".join(lines)
```

The test expects the fa windows text with Persian digits ("۱۶:۰۰"). The en text uses "–" from the `data.rule.window` template. Check that the fa unique rule renders as "هر تاریخ + ساعت فقط یک بار".

- [ ] **Step 5: Reuse it in the chat card**

In `backend/src/obot/telegram/data_chat.py`, delete `TYPE_NAMES` and rewrite `definition_text` so each line is `f"• {field_text(FieldDef.model_validate(f), lang)}"` for the added keys. Use `label_of` for the title, and import both from `obot.data.describe` (plus `FieldDef` from `obot.data.schema`). Run the existing chat tests to confirm the card text still contains the label and title.

- [ ] **Step 6: Run tests, lint, commit**

Run: `cd backend && uv run pytest tests/test_data_describe.py tests/test_data_chat.py -q && uv run pytest -q && uv run ruff check . && uv run ruff format --check .`

```bash
git add backend/src/obot/data/describe.py backend/src/obot/i18n.py backend/src/obot/telegram/data_chat.py backend/tests/test_data_describe.py
git commit -m "Describe collections in plain words for people

Co-Authored-By: <your model> <noreply@anthropic.com>"
```

---

### Task 2: Draft collections and syncing them to a bot

**Files:**
- Modify: `backend/src/obot/data/repository.py` (rename `_check_shared_unique` → `check_shared_unique`, update its two callers; add `template_collections`)
- Create: `backend/src/obot/factory/collections.py`
- Test: `backend/tests/test_factory_collections.py`

**Interfaces:**
- Consumes: `create_collection`, `update_collection_schema`, `collection_schema`, `check_shared_unique` (repository); `CollectionSchema`, `FieldDef`, `KEY_PATTERN` (schema); `InvalidSchema`, `DataError` (errors); `describe_error` (format).
- Produces:
  - `async template_collections(session, bot_id: int) -> list[Collection]`: the bot's own collections (`created_by_user_id IS NULL`), ordered by id.
  - `DraftField`, `DraftWindow`, `DraftHours`, `CollectionDraft` (pydantic models; `scope: Literal["personal", "shared"]`).
  - `to_schema(d: CollectionDraft, lang: str) -> CollectionSchema`: raises `InvalidSchema` with the collection key in the message.
  - `from_collection(c: Collection, lang: str) -> CollectionDraft`: visible fields only.
  - `merge_schema(old: CollectionSchema, new: CollectionSchema) -> CollectionSchema`: fields missing from `new` are kept hidden, and labels are merged per language.
  - `async sync_collections(session, pricing, bot: Bot, drafts: list[CollectionDraft], lang: str) -> list[str]`: the problems found, as "title: reason". The caller commits.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_factory_collections.py`:

```python
import pytest

from obot.data.errors import InvalidSchema
from obot.data.repository import add_record, template_collections
from obot.data.repository import collection_schema as schema_of
from obot.db.models import CollectionScope
from obot.factory.collections import (
    CollectionDraft,
    from_collection,
    sync_collections,
    to_schema,
)

BOOKINGS = CollectionDraft.model_validate(
    {
        "key": "appointments",
        "title": "نوبت‌ها",
        "scope": "shared",
        "fields": [
            {"key": "name", "label": "نام", "type": "text", "required": True},
            {"key": "phone", "label": "تلفن", "type": "phone"},
            {"key": "date", "label": "تاریخ", "type": "date", "required": True, "public": True},
            {"key": "time", "label": "ساعت", "type": "time", "required": True, "public": True},
        ],
        "unique": [["date", "time"]],
        "allowed_hours": {
            "date_field": "date",
            "time_field": "time",
            "windows": [{"weekdays": [5, 6, 0, 1, 2], "start": "16:00", "end": "20:00"}],
        },
    }
)


def test_to_schema_labels_in_the_draft_language() -> None:
    schema = to_schema(BOOKINGS, "fa")
    assert schema.field("name").label == {"fa": "نام"}
    assert schema.rules.unique == [["date", "time"]]
    assert schema.rules.allowed_hours.windows[0].weekdays == [5, 6, 0, 1, 2]


def test_to_schema_refuses_invalid_drafts() -> None:
    private_unique = BOOKINGS.model_copy(update={"unique": [["phone"]]})
    bad_key = BOOKINGS.model_copy(update={"key": "My List"})
    unknown_field = BOOKINGS.model_copy(update={"unique": [["nope"]]})
    for draft in (private_unique, bad_key, unknown_field):
        with pytest.raises(InvalidSchema) as caught:
            to_schema(draft, "fa")
        assert draft.key in str(caught.value)


async def test_sync_creates_then_extends_and_hides(data_world) -> None:
    w = data_world
    assert await sync_collections(w.session, w.pricing, w.bot, [BOOKINGS], "fa") == []
    [c] = await template_collections(w.session, w.bot.id)
    assert (c.key, c.scope, c.created_by_user_id) == ("appointments", CollectionScope.SHARED, None)
    space = await w.space(w.alice)
    await add_record(
        w.session, w.pricing, space, c,
        {"name": "Ali", "date": "2026-10-10", "time": "17:00"},
    )

    data = BOOKINGS.model_dump()
    data["title"] = "نوبت‌های مطب"
    data["fields"] = [f for f in data["fields"] if f["key"] != "phone"]
    data["fields"].append({"key": "reason", "label": "علت", "type": "text"})
    changed = CollectionDraft.model_validate(data)
    assert await sync_collections(w.session, w.pricing, w.bot, [changed], "fa") == []

    schema = schema_of(c)
    assert schema.field("phone").hidden is True  # dropped from the draft: hidden, kept
    assert schema.field("reason") is not None
    assert c.title["fa"] == "نوبت‌های مطب"
    assert [f.key for f in from_collection(c, "fa").fields] == ["name", "date", "time", "reason"]


async def test_sync_reports_problems_and_applies_the_rest(data_world) -> None:
    w = data_world
    await sync_collections(w.session, w.pricing, w.bot, [BOOKINGS], "fa")
    [c] = await template_collections(w.session, w.bot.id)
    await add_record(
        w.session, w.pricing, await w.space(w.alice), c,
        {"name": "Ali", "date": "2026-10-10", "time": "17:00"},
    )
    retyped = CollectionDraft.model_validate(
        {
            **BOOKINGS.model_dump(),
            "fields": [
                {**f.model_dump(), "type": "text"} if f.key == "date" else f.model_dump()
                for f in BOOKINGS.fields
            ],
            "allowed_hours": None,
        }
    )
    personal_now = BOOKINGS.model_copy(update={"key": "notes", "scope": "personal"})
    rescoped = CollectionDraft.model_validate(
        {**BOOKINGS.model_dump(), "key": "notes", "scope": "shared"}
    )

    problems = await sync_collections(
        w.session, w.pricing, w.bot, [retyped, personal_now], "fa"
    )
    assert len(problems) == 1 and problems[0].startswith("نوبت‌ها: ")
    assert "type" in problems[0]
    keys = sorted(c.key for c in await template_collections(w.session, w.bot.id))
    assert keys == ["appointments", "notes"]  # the valid one was still created

    problems = await sync_collections(w.session, w.pricing, w.bot, [rescoped], "fa")
    assert len(problems) == 1 and "personal" in problems[0]


async def test_sync_respects_the_owners_plan(data_world) -> None:
    w = data_world
    w.pricing.plan("free").max_data_collections = 1
    second = BOOKINGS.model_copy(update={"key": "waitlist", "title": "لیست انتظار"})

    problems = await sync_collections(w.session, w.pricing, w.bot, [BOOKINGS, second], "fa")

    assert len(problems) == 1 and problems[0].startswith("لیست انتظار: ")
```

`data_world` is the fixture from `backend/tests/conftest.py` (bot owned by tg 1001, users alice/bob).

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && uv run pytest tests/test_factory_collections.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'obot.factory.collections'`.

- [ ] **Step 3: Repository changes**

In `backend/src/obot/data/repository.py`, rename `_check_shared_unique` to `check_shared_unique` (definition and both call sites). Add after `list_collections`:

```python
async def template_collections(session: AsyncSession, bot_id: int) -> list[Collection]:
    """The bot's own collections (its template), for the factory and the owner's panel."""
    rows = await session.scalars(
        select(Collection)
        .where(Collection.bot_id == bot_id, Collection.created_by_user_id.is_(None))
        .order_by(Collection.id)
    )
    return list(rows)
```

- [ ] **Step 4: Write the module**

Create `backend/src/obot/factory/collections.py`:

```python
"""A bot's collections as the factory designs them (SPEC 8.8, P5-4).

The designer writes CollectionDraft values; sync_collections turns them into the bot's
template collections through obot.data. It never deletes data: a collection missing from a
draft is left alone and a field missing from it is hidden.
"""

import re
from typing import Literal

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from obot.billing.plans import Pricing
from obot.data.errors import DataError, InvalidSchema
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
    end: str = Field(description="HH:MM, exclusive")


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


def to_schema(d: CollectionDraft, lang: str) -> CollectionSchema:
    """The draft as a CollectionSchema; InvalidSchema names the collection's key."""
    if not re.fullmatch(KEY_PATTERN, d.key):
        raise InvalidSchema(f"{d.key}: the key must be a lowercase latin slug")
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
        check_shared_unique(CollectionScope(d.scope), schema)
    except ValidationError as e:
        reasons = "; ".join(err["msg"].removeprefix("Value error, ") for err in e.errors())
        raise InvalidSchema(f"{d.key}: {reasons}") from None
    except InvalidSchema as e:
        raise InvalidSchema(f"{d.key}: {e}") from None
    return schema


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
    language so other languages' labels are kept. Rules come from `new`."""
    new_fields = new.field_map()
    fields = []
    for f in old.fields:
        n = new_fields.get(f.key)
        if n is None:
            fields.append(f.model_copy(update={"hidden": True}))
        else:
            fields.append(n.model_copy(update={"label": {**f.label, **n.label}}))
    old_keys = old.field_map()
    fields += [n for n in new.fields if n.key not in old_keys]
    return CollectionSchema.model_validate(
        {
            "fields": [f.model_dump(mode="json") for f in fields],
            "rules": new.rules.model_dump(mode="json"),
        }
    )


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
                schema = to_schema(d, lang)
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
        except DataError as e:
            reason = describe_error(e).removeprefix("ERROR: ").rstrip(".")
            problems.append(f"{d.title}: {reason}")
    return problems
```

The rescope error text must contain "personal" so the test can find it (it does). The type-change problem text comes from `check_evolution` ("…the type of a field cannot change"), which contains "type".

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/test_factory_collections.py tests/test_data_collections.py tests/test_data_records.py -q`
Expected: all PASS. If a savepoint rollback leaves `existing[...]` objects expired, re-read the collection inside the loop instead of using the dict (`template_collections` per draft is fine for a handful of collections). Note it in the report if you do.

- [ ] **Step 6: Lint and commit**

Run: `cd backend && uv run pytest -q && uv run ruff check . && uv run ruff format --check .`

```bash
git add backend/src/obot/data/repository.py backend/src/obot/factory/collections.py backend/tests/test_factory_collections.py
git commit -m "Turn drafted collections into a bot's template without losing data

Co-Authored-By: <your model> <noreply@anthropic.com>"
```

---

### Task 3: The designer, the draft card and Create/Save

**Files:**
- Modify: `backend/src/obot/factory/designer.py`, `backend/src/obot/factory/drafts.py`, `backend/src/obot/telegram/factory.py`, `backend/src/obot/i18n.py`
- Test: `backend/tests/test_factory.py`

**Interfaces:**
- Consumes: Task 1 `collection_text`; Task 2 `CollectionDraft`, `to_schema`, `from_collection`, `sync_collections`, `template_collections`.
- Produces:
  - `BotDraft.collections: list[CollectionDraft]`, defaulting to empty.
  - `check_output(output: DesignerOutput) -> None`, which raises `ModelRetry`. `design` registers it as an output validator.
  - `async apply_draft(session, row, bot, pricing) -> list[str]`.
  - `render_card` shows a Data section.
  - Create/Save report problems through `factory.data_problems`.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_factory.py`:

```python
from pathlib import Path

import pytest
from pydantic_ai import ModelRetry

from obot.billing.plans import Pricing
from obot.data.repository import template_collections
from obot.factory.collections import CollectionDraft
from obot.factory.designer import DesignerOutput, check_output
from tests.test_factory_collections import BOOKINGS

PLANS = Pricing.load(Path(__file__).parent.parent / "plans.yaml")
DENTIST = DRAFT.model_copy(update={"name": "Dr. Sara", "collections": [BOOKINGS]})


def test_check_output_asks_the_model_to_fix_invalid_collections() -> None:
    ok = DesignerOutput(action="draft", draft=DENTIST, reply="ok")
    check_output(ok)

    broken = BOOKINGS.model_copy(update={"unique": [["phone"]]})
    bad = DesignerOutput(
        action="draft", draft=DRAFT.model_copy(update={"collections": [broken]}), reply="ok"
    )
    with pytest.raises(ModelRetry) as caught:
        check_output(bad)
    assert "appointments" in str(caught.value)

    twice = DesignerOutput(
        action="draft", draft=DRAFT.model_copy(update={"collections": [BOOKINGS, BOOKINGS]}),
        reply="ok",
    )
    with pytest.raises(ModelRetry):
        check_output(twice)


async def test_design_accepts_collections() -> None:
    model = TestModel(
        custom_output_args={
            "action": "draft",
            "draft": DENTIST.model_dump(mode="json"),
            "questions": [],
            "reply": "ساختمش!",
        }
    )
    result = await design(model, "دندانپزشکم، بیمارها نوبت بگیرند")
    assert result.output.draft.collections[0].key == "appointments"


async def test_card_shows_the_data_section(session) -> None:
    owner = await get_or_create_user(session, 1001)
    row = await start_draft(session, owner, DENTIST, last_reply=None)

    card = render_card("fa", row, target_username=None)

    assert "📋" in card and "نوبت‌ها" in card
    assert "همه ثبت می‌کنند، فقط شما همه را می‌بینید" in card
    assert "هر تاریخ + ساعت فقط یک بار" in card


async def test_applying_a_draft_creates_its_collections(session, cipher) -> None:
    bot = await make_bot(session, cipher)
    row = await start_draft(session, bot.owner, DENTIST, last_reply=None)

    problems = await apply_draft(session, row, bot, PLANS)

    assert problems == []
    [c] = await template_collections(session, bot.id)
    assert c.key == "appointments"
    [existing] = await existing_bot_drafts(session, bot.owner)
    assert existing.draft.collections[0].key == "appointments"
```

Update the two existing `apply_draft(session, …, bot)` calls in this file to pass `PLANS` as a fourth argument (`await apply_draft(session, taken, bot, PLANS)`). Also add `BOOKINGS`'s import location to the import block at the top instead of mid-file, so ruff E402 passes.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && uv run pytest tests/test_factory.py -q`
Expected: FAIL. The new tests fail with `cannot import name 'check_output'`, and the old ones fail on the extra `apply_draft` argument.

- [ ] **Step 3: The designer**

In `backend/src/obot/factory/designer.py`:
- import `from pydantic_ai import Agent, ModelRetry`, and `from obot.data.errors import InvalidSchema` and `from obot.factory.collections import CollectionDraft, to_schema`;
- add to `BotDraft`:

```python
    collections: list[CollectionDraft] = Field(
        default_factory=list,
        description=(
            "Lists the bot keeps (bookings, orders, expenses...). Empty for bots that only "
            "talk or fetch information."
        ),
    )
```

- add after `DesignerOutput`:

```python
def check_output(output: DesignerOutput) -> None:
    """Ask the model to fix a draft whose collections obot.data would refuse."""
    draft = output.draft
    if draft is None:
        return
    keys = [c.key for c in draft.collections]
    if len(set(keys)) != len(keys):
        raise ModelRetry("collection keys must be unique within a bot")
    for c in draft.collections:
        try:
            to_schema(c, draft.language)
        except InvalidSchema as e:
            raise ModelRetry(f"fix collection {e}") from None
```

- in `design`, after creating the agent:

```python
    @agent.output_validator
    def _valid(output: DesignerOutput) -> DesignerOutput:
        check_output(output)
        return output
```

- in `_instructions()`, add this paragraph after the scheduling paragraph:

```text
When the bot must keep things people send (bookings, orders, registrations, expenses,
lists), add collections to the draft. 'shared' collections collect from everyone and only
the owner sees all records (bookings, orders, sign-ups); mark only the fields that show a
slot is taken as public (e.g. date and time), never names, phones or notes, and make
unique rules only over public fields (e.g. [['date','time']] so a slot is booked once). Add
allowed_hours when bookings are only possible at certain times (weekdays 0 = Monday …
5 = Saturday, 6 = Sunday). 'personal' collections give each user their own records
(expense book, habit tracker). If users should invent their own lists in chat, add the
personal_data tool instead. Keys are lowercase latin slugs; titles and labels are in the
bot's language. Collections cannot notify or remind anyone yet: never promise that. When
changing an existing bot, keep its collections and their keys unless the user asks
otherwise; leaving one out never deletes its data.
```

- [ ] **Step 4: Drafts**

In `backend/src/obot/factory/drafts.py`:
- `apply_draft(session, row, bot, pricing) -> list[str]`: after the existing assignments and before `row.state = DraftState.APPLIED`, add `problems = await sync_collections(session, pricing, bot, draft.collections, draft.language)`; return `problems` at the end. Import `Pricing` and `sync_collections`.
- In `existing_bot_drafts`, add `collections=[from_collection(c, persona.get("language", "en")) for c in await template_collections(session, bot.id)]` to the `BotDraft(...)` call.

- [ ] **Step 5: The card and Create/Save**

In `backend/src/obot/i18n.py` add:
- en: `"card.data": "Data"` and `"factory.data_problems": "⚠️ Some lists were not changed:\n{problems}"`
- fa: `"card.data": "داده‌ها"` and `"factory.data_problems": "⚠️ این فهرست‌ها تغییر نکردند:\n{problems}"`

In `backend/src/obot/telegram/factory.py`:
- In `render_card`, after the tools block, add:

```python
    if draft.collections:
        lines += ["", f"📋 {t(lang, 'card.data')}:"]
        for c in draft.collections:
            try:
                schema = to_schema(c, draft.language)
            except InvalidSchema:
                continue  # the designer's validator makes this rare; never break the card
            lines.append(
                html.escape(collection_text(c.title, CollectionScope(c.scope), schema, lang))
            )
```

  Import `to_schema` from `obot.factory.collections`, `InvalidSchema`, `collection_text` from `obot.data.describe`, and `CollectionScope`.

- In `on_save` and `on_managed_bot`, pass `runtime.pricing` to `apply_draft`, keep the returned `problems`, and when it is non-empty send `t(lang, "factory.data_problems", problems="\n".join(f"• {html.escape(p)}" for p in problems))` with `query.message.answer(...)` (on_save) or the same send path the handler already uses for its confirmation message (on_managed_bot; read the handler and use its existing reply mechanism to the owner).

- [ ] **Step 6: Run tests, lint, commit**

Run: `cd backend && uv run pytest tests/test_factory.py tests/test_factory_collections.py -q && uv run pytest -q && uv run ruff check . && uv run ruff format --check .`

```bash
git add backend/src/obot/factory/designer.py backend/src/obot/factory/drafts.py backend/src/obot/telegram/factory.py backend/src/obot/i18n.py backend/tests/test_factory.py
git commit -m "Let the factory design a bot's lists and show them on the draft card

Co-Authored-By: <your model> <noreply@anthropic.com>"
```

---

## Spec coverage (P5-4)

| Spec ۸٫۸ requirement | Task |
|---|---|
| Factory guesses fields and rules from the conversation | 3 (designer + validator) |
| «داده‌ها» section in the draft card, in plain words | 1, 3 |
| «تغییر بده» changes collections like the rest of the draft | 3 (existing collections in the designer's context; sync on Save) |
| Template collections count against the owner's plan | 2 (`create_collection`) |
| Only additive changes once records exist; a removed field is hidden | 2 (`merge_schema` + `check_evolution`) |
| Shared unique rules only over public fields | 2 (`check_shared_unique`), 3 (retry) |
| Events (notify/remind) | P5-5; the designer must not promise them (Task 3 instructions) |
