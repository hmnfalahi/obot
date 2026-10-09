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
