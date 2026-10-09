from decimal import Decimal

import pytest
from sqlalchemy import func, select

from obot.data.errors import DuplicateRecord, InvalidSchema
from obot.data.repository import add_record, template_collections
from obot.data.repository import collection_schema as schema_of
from obot.db.models import CollectionScope, RecordKey
from obot.factory.collections import (
    CollectionDraft,
    DraftWindow,
    Problem,
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
        w.session,
        w.pricing,
        space,
        c,
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
        w.session,
        w.pricing,
        await w.space(w.alice),
        c,
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

    problems = await sync_collections(w.session, w.pricing, w.bot, [retyped, personal_now], "fa")
    assert len(problems) == 1 and problems[0].title == "نوبت‌ها"
    assert problems[0] == Problem("نوبت‌ها", "breaks_records", change="type_change", field="تاریخ")
    keys = sorted(c.key for c in await template_collections(w.session, w.bot.id))
    assert keys == ["appointments", "notes"]  # the valid one was still created

    problems = await sync_collections(w.session, w.pricing, w.bot, [rescoped], "fa")
    assert problems == [Problem(BOOKINGS.title, "kind_change")]


async def test_sync_respects_the_owners_plan(data_world) -> None:
    w = data_world
    w.pricing.plan("free").max_data_collections = 1
    second = BOOKINGS.model_copy(update={"key": "waitlist", "title": "لیست انتظار"})

    problems = await sync_collections(w.session, w.pricing, w.bot, [BOOKINGS, second], "fa")

    assert problems == [Problem("لیست انتظار", "limit")]


async def test_sync_too_many_fields_is_one_problem(data_world) -> None:
    w = data_world
    await sync_collections(w.session, w.pricing, w.bot, [BOOKINGS], "fa")
    data = BOOKINGS.model_dump()
    data["fields"] = [{"key": f"f{i}", "label": f"f{i}", "type": "text"} for i in range(28)] + [
        data["fields"][2],
        data["fields"][3],
    ]
    data["unique"] = [["date", "time"]]
    big = CollectionDraft.model_validate(data)
    other = BOOKINGS.model_copy(update={"key": "waitlist", "title": "لیست انتظار"})

    problems = await sync_collections(w.session, w.pricing, w.bot, [big, other], "fa")

    assert len(problems) == 1
    assert problems[0] == Problem("نوبت‌ها", "too_many_fields", hidden=2)
    keys = sorted(c.key for c in await template_collections(w.session, w.bot.id))
    assert keys == ["appointments", "waitlist"]


async def test_sync_keeps_min_max_and_unhides(data_world) -> None:
    w = data_world
    draft = CollectionDraft.model_validate(
        {
            "key": "expenses",
            "title": "خرج",
            "scope": "personal",
            "fields": [
                {"key": "amount", "label": "مبلغ", "type": "money", "currency": "IRT"},
                {"key": "note", "label": "یادداشت", "type": "text"},
            ],
        }
    )
    await sync_collections(w.session, w.pricing, w.bot, [draft], "fa")
    [c] = await template_collections(w.session, w.bot.id)
    schema = schema_of(c)
    stored = schema.field("amount").model_copy(update={"min": Decimal(1), "max": Decimal(9)})
    from obot.data.repository import update_collection_schema

    await update_collection_schema(
        w.session, c, schema.model_copy(update={"fields": [stored, schema.field("note")]})
    )

    no_note = draft.model_copy(
        update={"fields": [f.model_copy(update={"currency": None}) for f in draft.fields[:1]]}
    )
    assert await sync_collections(w.session, w.pricing, w.bot, [no_note], "fa") == []
    assert schema_of(c).field("note").hidden is True
    assert schema_of(c).field("amount").max == Decimal(9)
    assert schema_of(c).field("amount").currency == "IRT"

    assert await sync_collections(w.session, w.pricing, w.bot, [draft], "fa") == []
    assert schema_of(c).field("note").hidden is False


async def test_sync_plan_limit_wording(data_world) -> None:
    w = data_world
    w.pricing.plan("free").max_data_collections = 0
    problems = await sync_collections(w.session, w.pricing, w.bot, [BOOKINGS], "fa")
    assert problems == [Problem("نوبت‌ها", "limit")]


async def _booked(w):
    await sync_collections(w.session, w.pricing, w.bot, [BOOKINGS], "fa")
    [c] = await template_collections(w.session, w.bot.id)
    space = await w.space(w.alice)
    await add_record(
        w.session, w.pricing, space, c, {"name": "Ali", "date": "2026-10-10", "time": "17:00"}
    )
    return c, space


async def test_resync_without_rules_keeps_them(data_world) -> None:
    w = data_world
    c, space = await _booked(w)
    bare = BOOKINGS.model_copy(update={"unique": [], "allowed_hours": None})

    assert await sync_collections(w.session, w.pricing, w.bot, [bare], "fa") == []

    rules = schema_of(c).rules
    assert rules.unique == [["date", "time"]]
    assert rules.allowed_hours.windows[0].weekdays == [5, 6, 0, 1, 2]
    keys = await w.session.scalar(
        select(func.count()).select_from(RecordKey).where(RecordKey.collection_id == c.id)
    )
    assert keys == 1
    with pytest.raises(DuplicateRecord):
        await add_record(
            w.session, w.pricing, space, c, {"name": "Sara", "date": "2026-10-10", "time": "17:00"}
        )


async def test_resync_with_a_reordered_unique_group_is_the_same_rule(data_world) -> None:
    w = data_world
    c, _ = await _booked(w)
    reordered = BOOKINGS.model_copy(update={"unique": [["time", "date"]]})

    assert await sync_collections(w.session, w.pricing, w.bot, [reordered], "fa") == []
    assert schema_of(c).rules.unique == [["date", "time"]]


async def test_resync_with_new_windows_replaces_the_old_ones(data_world) -> None:
    w = data_world
    c, _ = await _booked(w)
    hours = BOOKINGS.allowed_hours.model_copy(
        update={"windows": [DraftWindow(weekdays=[5], start="10:00", end="18:00")]}
    )
    moved = BOOKINGS.model_copy(update={"allowed_hours": hours})

    assert await sync_collections(w.session, w.pricing, w.bot, [moved], "fa") == []
    [window] = schema_of(c).rules.allowed_hours.windows
    assert (window.weekdays, f"{window.start:%H:%M}") == ([5], "10:00")
