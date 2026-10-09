from decimal import Decimal

import pytest

from obot.data.errors import InvalidQuery
from obot.data.query import (
    Filter,
    FilterOp,
    Metric,
    Period,
    SummaryRow,
    availability,
    find_records,
    summarize,
)
from obot.data.repository import add_record, create_collection
from obot.data.schema import CollectionSchema
from obot.db.models import CollectionScope

EXPENSES = CollectionSchema.model_validate(
    {
        "fields": [
            {"key": "amount", "label": {"en": "Amount"}, "type": "money", "required": True},
            {
                "key": "category",
                "label": {"en": "Category"},
                "type": "choice",
                "options": ["food", "rent", "travel"],
            },
            {"key": "day", "label": {"en": "Day"}, "type": "date"},
            {"key": "note", "label": {"en": "Note"}, "type": "text"},
        ]
    }
)
BOOKINGS = CollectionSchema.model_validate(
    {
        "fields": [
            {"key": "name", "label": {"en": "Name"}, "type": "text", "required": True},
            {"key": "date", "label": {"en": "Date"}, "type": "date", "public": True},
            {"key": "time", "label": {"en": "Time"}, "type": "time", "public": True},
        ]
    }
)


async def setup_expenses(w):
    c = await create_collection(
        w.session,
        w.pricing,
        bot=w.bot,
        key="expenses",
        title={"en": "E"},
        scope=CollectionScope.PERSONAL,
        schema=EXPENSES,
    )
    alice = await w.space(w.alice)
    for amount, category, day, note in [
        ("250000", "food", "2026-09-30", "Lunch with Sara"),
        ("1000", "food", "2026-10-01", "bread"),
        ("9000000", "rent", "2026-10-01", None),
        ("40.5", "travel", "2026-10-08", "taxi"),
    ]:
        data = {"amount": amount, "category": category, "day": day}
        if note:
            data["note"] = note
        await add_record(w.session, w.pricing, alice, c, data)
    # Bob's records must never show up in Alice's results.
    await add_record(
        w.session, w.pricing, await w.space(w.bob), c, {"amount": 7, "category": "food"}
    )
    return c, alice


async def test_find_with_filters_and_sort(data_world) -> None:
    w = data_world
    c, alice = await setup_expenses(w)

    found = await find_records(
        w.session,
        alice,
        c,
        [Filter("amount", FilterOp.GTE, "۱۰۰۰")],
        sort="amount",
        descending=False,
    )
    # Numeric, not text, order: 1000 < 250000 < 9000000
    assert [r.data["amount"] for r in found] == ["1000", "250000", "9000000"]

    found = await find_records(w.session, alice, c, [Filter("note", FilterOp.CONTAINS, "sara")])
    assert [r.data["note"] for r in found] == ["Lunch with Sara"]

    found = await find_records(
        w.session, alice, c, [Filter("category", FilterOp.IN, ["rent", "travel"])]
    )
    assert len(found) == 2

    found = await find_records(w.session, alice, c, [Filter("day", FilterOp.LT, "2026-10-01")])
    assert len(found) == 1


async def test_find_rejects_unknown_fields_and_bad_values(data_world) -> None:
    w = data_world
    c, alice = await setup_expenses(w)
    with pytest.raises(InvalidQuery):
        await find_records(w.session, alice, c, [Filter("colour", FilterOp.EQ, "x")])
    with pytest.raises(InvalidQuery):
        await find_records(w.session, alice, c, [Filter("amount", FilterOp.EQ, "lots")])
    with pytest.raises(InvalidQuery):
        await find_records(w.session, alice, c, [Filter("amount", FilterOp.CONTAINS, "1")])


async def test_summaries(data_world) -> None:
    w = data_world
    c, alice = await setup_expenses(w)

    total = await summarize(w.session, alice, c, Metric.SUM, "amount")
    assert total == [SummaryRow(None, Decimal("9251040.5"))]

    by_category = await summarize(w.session, alice, c, Metric.SUM, "amount", group_by="category")
    assert by_category == [
        SummaryRow("food", Decimal("251000")),
        SummaryRow("rent", Decimal("9000000")),
        SummaryRow("travel", Decimal("40.5")),
    ]

    by_month = await summarize(w.session, alice, c, Metric.COUNT, period=Period.MONTH)
    assert by_month == [SummaryRow("2026-09", Decimal(1)), SummaryRow("2026-10", Decimal(3))]

    food = await summarize(
        w.session,
        alice,
        c,
        Metric.COUNT,
        filters=[Filter("category", FilterOp.EQ, "food")],
    )
    assert food == [SummaryRow(None, Decimal(2))]

    with pytest.raises(InvalidQuery):
        await summarize(w.session, alice, c, Metric.SUM, "note")
    with pytest.raises(InvalidQuery):
        await summarize(w.session, alice, c, Metric.COUNT, group_by="category", period=Period.DAY)


async def test_availability_shows_only_public_fields(data_world) -> None:
    w = data_world
    c = await create_collection(
        w.session,
        w.pricing,
        bot=w.bot,
        key="bookings",
        title={"en": "B"},
        scope=CollectionScope.SHARED,
        schema=BOOKINGS,
    )
    await add_record(
        w.session,
        w.pricing,
        await w.space(w.alice),
        c,
        {"name": "Alice", "date": "2026-10-10", "time": "17:00"},
    )
    bob = await w.space(w.bob)

    assert await find_records(w.session, bob, c) == []
    taken = await availability(w.session, bob, c, [Filter("date", FilterOp.EQ, "2026-10-10")])
    assert taken == [{"date": "2026-10-10", "time": "17:00"}]
    # Filtering on a private field would let Bob probe Alice's name.
    with pytest.raises(InvalidQuery):
        await availability(w.session, bob, c, [Filter("name", FilterOp.EQ, "Alice")])


async def test_search_and_offset(data_world) -> None:
    w = data_world
    c, alice = await setup_expenses(w)

    found = await find_records(w.session, alice, c, search="SARA")
    assert [r.data.get("note") for r in found] == ["Lunch with Sara"]

    first = await find_records(w.session, alice, c, sort="amount", descending=False, limit=2)
    second = await find_records(
        w.session, alice, c, sort="amount", descending=False, limit=2, offset=2
    )
    assert [r.data["amount"] for r in first + second] == ["40.5", "1000", "250000", "9000000"]
