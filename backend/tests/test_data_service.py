from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from obot.data.query import find_records
from obot.data.repository import (
    collection_schema,
    create_collection,
    get_collection,
    update_collection_schema,
)
from obot.data.schema import CollectionSchema
from obot.data.service import DataService
from obot.db.models import CollectionScope

EXPENSES = CollectionSchema.model_validate(
    {
        "fields": [
            {"key": "amount", "label": {"en": "Amount"}, "type": "money", "required": True},
            {
                "key": "category",
                "label": {"en": "Category"},
                "type": "choice",
                "options": ["food", "rent"],
            },
        ]
    }
)
BOOKINGS = CollectionSchema.model_validate(
    {
        "fields": [
            {"key": "name", "label": {"en": "Name"}, "type": "text", "required": True},
            {"key": "date", "label": {"en": "Date"}, "type": "date", "public": True},
            {"key": "time", "label": {"en": "Time"}, "type": "time", "public": True},
        ],
        "rules": {"unique": [["date", "time"]]},
    }
)


@pytest.fixture
async def world(data_world):
    w = data_world
    for key, scope, schema in (
        ("expenses", CollectionScope.PERSONAL, EXPENSES),
        ("bookings", CollectionScope.SHARED, BOOKINGS),
    ):
        await create_collection(
            w.session,
            w.pricing,
            bot=w.bot,
            key=key,
            title={"en": key},
            scope=scope,
            schema=schema,
        )
    await w.session.commit()
    w.sessions = async_sessionmaker(w.session.bind, expire_on_commit=False)
    return w


def service(w, user, **kwargs) -> DataService:
    return DataService(w.sessions, w.pricing, w.bot.id, user.id, **kwargs)


async def test_add_find_and_summarize(world) -> None:
    alice = service(world, world.alice)

    first = await alice.add("expenses", {"amount": "۱۲۰", "category": "food"})
    await alice.add("expenses", {"amount": 1000, "category": "food"})
    await alice.add("expenses", {"amount": 5, "category": "rent"})

    assert first.startswith("OK: saved as #") and '"amount": "120"' in first
    found = await alice.find("expenses", [{"field": "category", "op": "eq", "value": "rent"}])
    assert found.count("#") == 1 and '"amount": "5"' in found
    assert await alice.summarize("expenses", "sum", "amount") == "sum: 1125"
    by_category = await alice.summarize("expenses", "sum", "amount", group_by="category")
    assert by_category == "sum by group:\nfood: 1120\nrent: 5"
    # Committed: a fresh session sees the records; Bob does not.
    assert await service(world, world.bob).find("expenses") == "No records."


async def test_errors_come_back_as_text(world) -> None:
    alice = service(world, world.alice)
    assert (await alice.add("expenses", {"amount": "lots"})).startswith(
        "ERROR: invalid values: amount"
    )
    assert "no collection named 'nope'" in await alice.find("nope")
    bad_op = await alice.find("expenses", [{"field": "amount", "op": "like", "value": 1}])
    assert bad_op.startswith("ERROR: ")
    assert (await alice.summarize("expenses", "median", "amount")).startswith("ERROR: ")
    assert "no record #999" in await alice.update("expenses", 999, {"amount": 1})


async def test_shared_bookings_notice_and_availability(world) -> None:
    notice = AsyncMock()
    alice = service(world, world.alice, notice_shared=notice)
    bob = service(world, world.bob, notice_shared=AsyncMock())
    owner_notice = AsyncMock()
    owner = service(world, world.owner, notice_shared=owner_notice)

    ok = await alice.add("bookings", {"name": "Alice", "date": "2026-10-10", "time": "17:00"})
    taken = await bob.add("bookings", {"name": "Bob", "date": "2026-10-10", "time": "17:00"})

    assert ok.startswith("OK:") and notice.await_count == 1
    assert "already exists" in taken
    assert await bob.find("bookings") == "No records."
    assert await bob.find("bookings", availability=True) == (
        'Taken: {"date": "2026-10-10", "time": "17:00"}'
    )
    await owner.add("bookings", {"name": "Walk-in", "date": "2026-10-10", "time": "18:00"})
    owner_notice.assert_not_awaited()  # the owner is not told they see their own data
    assert (await owner.find("bookings")).count("#") == 2


async def test_delete_asks_for_confirmation(world) -> None:
    confirm = AsyncMock(return_value="Asked the user to confirm with a button.")
    alice = service(world, world.alice, confirm_delete=confirm)
    saved = await alice.add("expenses", {"amount": 7})
    record_id = int(saved.split("#")[1].split(":")[0])

    assert await alice.delete("expenses", record_id) == "Asked the user to confirm with a button."
    collection, record = confirm.await_args.args
    assert (collection.key, record.id) == ("expenses", record_id)
    # Nothing is deleted until the person presses the button.
    async with world.sessions() as s:
        space = await world.space(world.alice)
        c = await get_collection(s, space, "expenses")
        assert [r.id for r in await find_records(s, space, c)] == [record_id]

    no_button = service(world, world.alice)
    assert (await no_button.delete("expenses", record_id)).startswith("ERROR: ")
    assert "no record" in await service(world, world.bob, confirm_delete=confirm).delete(
        "expenses", record_id
    )


async def test_note_lists_visible_collections(world, data_world) -> None:
    async with world.sessions() as s:
        note = await service(world, world.alice).note(s)
    assert 'expenses "expenses"' in note and 'bookings "bookings"' in note


async def test_no_note_without_collections(data_world) -> None:
    w = data_world
    await w.session.commit()
    sessions = async_sessionmaker(w.session.bind, expire_on_commit=False)
    async with sessions() as s:
        assert await DataService(sessions, w.pricing, w.bot.id, w.alice.id).note(s) is None


async def test_find_hides_hidden_fields(world) -> None:
    alice = service(world, world.alice)
    await alice.add("expenses", {"amount": 3, "category": "food"})
    async with world.sessions() as s:
        space = await world.space(world.alice)
        c = await get_collection(s, space, "expenses")
        schema = collection_schema(c).model_dump(mode="json")
        for f in schema["fields"]:
            if f["key"] == "category":
                f["hidden"] = True
        await update_collection_schema(s, c, CollectionSchema.model_validate(schema))
        await s.commit()
    found = await alice.find("expenses")
    assert '"amount": "3"' in found and "food" not in found
