import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker

from obot.data.errors import (
    DuplicateRecord,
    LimitReached,
    NotAllowed,
    OutsideAllowedHours,
    RecordInvalid,
    RecordNotFound,
    SchemaChangeRejected,
    StaleRecord,
)
from obot.data.repository import (
    add_record,
    create_collection,
    delete_record,
    get_record,
    purge_deleted_records,
    update_collection_schema,
    update_record,
)
from obot.data.schema import CollectionSchema
from obot.db.models import CollectionScope, Record

BOOKINGS = CollectionSchema.model_validate(
    {
        "fields": [
            {"key": "name", "label": {"en": "Name"}, "type": "text", "required": True},
            {
                "key": "date",
                "label": {"en": "Date"},
                "type": "date",
                "required": True,
                "public": True,
            },
            {
                "key": "time",
                "label": {"en": "Time"},
                "type": "time",
                "required": True,
                "public": True,
            },
        ],
        "rules": {
            "unique": [["date", "time"]],
            "allowed_hours": {
                "date_field": "date",
                "time_field": "time",
                "windows": [{"weekdays": [5, 6, 0, 1, 2], "start": "16:00", "end": "20:00"}],
            },
        },
    }
)
EXPENSES = CollectionSchema.model_validate(
    {
        "fields": [
            {"key": "amount", "label": {"en": "Amount"}, "type": "money", "required": True},
            {"key": "note", "label": {"en": "Note"}, "type": "text"},
        ],
        "rules": {"unique": [["note"]]},
    }
)
SATURDAY = "2026-10-10"


async def collection(w, key, scope, schema):
    return await create_collection(
        w.session, w.pricing, bot=w.bot, key=key, title={"en": key}, scope=scope, schema=schema
    )


async def test_personal_records_are_private(data_world) -> None:
    w = data_world
    c = await collection(w, "expenses", CollectionScope.PERSONAL, EXPENSES)
    alice, bob = await w.space(w.alice), await w.space(w.bob)
    r = await add_record(w.session, w.pricing, alice, c, {"amount": "120"})

    assert (await get_record(w.session, alice, c, r.id)).data == {"amount": "120"}
    with pytest.raises(RecordNotFound):
        await get_record(w.session, bob, c, r.id)
    with pytest.raises(RecordNotFound):
        await update_record(w.session, w.pricing, bob, c, r.id, {"amount": 1})
    with pytest.raises(RecordNotFound):
        await delete_record(w.session, bob, c, r.id)
    # The owner of the bot does not see personal records either.
    with pytest.raises(RecordNotFound):
        await get_record(w.session, await w.space(w.owner), c, r.id)


async def test_personal_unique_rules_apply_per_person(data_world) -> None:
    w = data_world
    c = await collection(w, "expenses", CollectionScope.PERSONAL, EXPENSES)
    alice, bob = await w.space(w.alice), await w.space(w.bob)
    await add_record(w.session, w.pricing, alice, c, {"amount": 1, "note": "rent"})
    await add_record(w.session, w.pricing, bob, c, {"amount": 1, "note": "rent"})
    with pytest.raises(DuplicateRecord):
        await add_record(w.session, w.pricing, alice, c, {"amount": 2, "note": "rent"})


async def test_shared_records_owner_sees_all_others_their_own(data_world) -> None:
    w = data_world
    c = await collection(w, "bookings", CollectionScope.SHARED, BOOKINGS)
    alice, bob, owner = await w.space(w.alice), await w.space(w.bob), await w.space(w.owner)
    r = await add_record(
        w.session, w.pricing, alice, c, {"name": "Alice", "date": SATURDAY, "time": "17:00"}
    )

    with pytest.raises(RecordNotFound):
        await get_record(w.session, bob, c, r.id)
    assert (await get_record(w.session, owner, c, r.id)).data["name"] == "Alice"
    # The owner may change anyone's booking; Bob may not book the same slot.
    await update_record(w.session, w.pricing, owner, c, r.id, {"time": "18:00"})
    with pytest.raises(DuplicateRecord) as caught:
        await add_record(
            w.session, w.pricing, bob, c, {"name": "Bob", "date": SATURDAY, "time": "18:00"}
        )
    assert caught.value.fields == ["date", "time"]
    # The old slot was released by the update.
    await add_record(
        w.session, w.pricing, bob, c, {"name": "Bob", "date": SATURDAY, "time": "17:00"}
    )


async def test_group_records_belong_to_the_chat(data_world) -> None:
    w = data_world
    c = await collection(w, "shopping", CollectionScope.GROUP, EXPENSES)
    alice_g1 = await w.space(w.alice, tg_chat_id=-1)
    bob_g1 = await w.space(w.bob, tg_chat_id=-1)
    bob_g1_admin = await w.space(w.bob, tg_chat_id=-1, is_group_admin=True)
    bob_g2 = await w.space(w.bob, tg_chat_id=-2)
    r = await add_record(w.session, w.pricing, alice_g1, c, {"amount": 3, "note": "milk"})

    assert (await get_record(w.session, bob_g1, c, r.id)).tg_chat_id == -1
    with pytest.raises(RecordNotFound):
        await get_record(w.session, bob_g2, c, r.id)
    with pytest.raises(NotAllowed):
        await update_record(w.session, w.pricing, bob_g1, c, r.id, {"amount": 4})
    await update_record(w.session, w.pricing, bob_g1_admin, c, r.id, {"amount": 4})
    # Unique rules apply per chat.
    await add_record(w.session, w.pricing, bob_g2, c, {"amount": 1, "note": "milk"})


async def test_rules_and_validation(data_world) -> None:
    w = data_world
    c = await collection(w, "bookings", CollectionScope.SHARED, BOOKINGS)
    alice = await w.space(w.alice)
    with pytest.raises(OutsideAllowedHours):
        await add_record(
            w.session, w.pricing, alice, c, {"name": "A", "date": SATURDAY, "time": "21:00"}
        )
    with pytest.raises(RecordInvalid):
        await add_record(w.session, w.pricing, alice, c, {"name": "A"})


async def test_versions_and_soft_delete(data_world) -> None:
    w = data_world
    c = await collection(w, "expenses", CollectionScope.PERSONAL, EXPENSES)
    alice = await w.space(w.alice)
    r = await add_record(w.session, w.pricing, alice, c, {"amount": 1, "note": "a"})
    r = await update_record(w.session, w.pricing, alice, c, r.id, {"amount": 2})
    assert (r.version, r.data) == (2, {"amount": "2", "note": "a"})
    with pytest.raises(StaleRecord):
        await update_record(w.session, w.pricing, alice, c, r.id, {"amount": 3}, expected_version=1)

    await delete_record(w.session, alice, c, r.id)
    with pytest.raises(RecordNotFound):
        await get_record(w.session, alice, c, r.id)
    # Its unique key is released.
    await add_record(w.session, w.pricing, alice, c, {"amount": 1, "note": "a"})

    old = datetime.now(UTC) - timedelta(days=31)
    await w.session.execute(update(Record).where(Record.id == r.id).values(deleted_at=old))
    assert await purge_deleted_records(w.session, 30) == 1


async def test_limits(data_world) -> None:
    w = data_world
    c = await collection(w, "expenses", CollectionScope.PERSONAL, EXPENSES)
    alice = await w.space(w.alice)
    plan = w.pricing.plan("free")  # the fixture's own Pricing; safe to change
    plan.max_record_bytes = 50
    with pytest.raises(LimitReached) as caught:
        await add_record(w.session, w.pricing, alice, c, {"amount": 1, "note": "x" * 100})
    assert caught.value.limit == "record_bytes"

    plan.max_record_bytes = 4096
    plan.max_data_records = 2
    await add_record(w.session, w.pricing, alice, c, {"amount": 1})
    await add_record(w.session, w.pricing, alice, c, {"amount": 2})
    with pytest.raises(LimitReached):
        await add_record(w.session, w.pricing, alice, c, {"amount": 3})
    # Bob's personal records are his own allowance.
    await add_record(w.session, w.pricing, await w.space(w.bob), c, {"amount": 1})


async def test_schema_change_rejected_once_records_exist(data_world) -> None:
    w = data_world
    c = await collection(w, "expenses", CollectionScope.PERSONAL, EXPENSES)
    await add_record(w.session, w.pricing, await w.space(w.alice), c, {"amount": 1})
    narrowed = CollectionSchema.model_validate({"fields": [EXPENSES.model_dump()["fields"][0]]})
    with pytest.raises(SchemaChangeRejected):
        await update_collection_schema(w.session, c, narrowed)


async def test_concurrent_bookings_of_one_slot(data_world) -> None:
    w = data_world
    c = await collection(w, "bookings", CollectionScope.SHARED, BOOKINGS)
    alice, bob = await w.space(w.alice), await w.space(w.bob)
    await w.session.commit()
    sessions = async_sessionmaker(w.session.bind, expire_on_commit=False)

    async def book(space, name):
        async with sessions() as s:
            try:
                await add_record(
                    s, w.pricing, space, c, {"name": name, "date": SATURDAY, "time": "17:00"}
                )
                await s.commit()
                return "ok"
            except DuplicateRecord:
                return "duplicate"

    results = await asyncio.gather(book(alice, "Alice"), book(bob, "Bob"))
    assert sorted(results) == ["duplicate", "ok"]


async def test_purge_job_commits(data_world) -> None:
    from obot.data.jobs import purge_records

    w = data_world
    c = await collection(w, "expenses", CollectionScope.PERSONAL, EXPENSES)
    alice = await w.space(w.alice)
    r = await add_record(w.session, w.pricing, alice, c, {"amount": 1})
    await delete_record(w.session, alice, c, r.id)
    old = datetime.now(UTC) - timedelta(days=31)
    await w.session.execute(update(Record).where(Record.id == r.id).values(deleted_at=old))
    await w.session.commit()

    await purge_records(async_sessionmaker(w.session.bind, expire_on_commit=False), 30)

    w.session.expunge_all()
    assert await w.session.get(Record, r.id) is None


async def other_bot_world(w, cipher):
    from obot.bots.service import ManagedBotInfo, register_managed_bot
    from obot.bots.users import join_bot
    from obot.data.space import space_for

    async def fetch_token(_: int) -> str:
        return "t"

    other = (
        await register_managed_bot(
            w.session, cipher, fetch_token, 1001, ManagedBotInfo(78, "datac", "C"), 10
        )
    ).bot
    user, _ = await join_bot(w.session, other, 2002, "fa")
    await w.session.flush()
    return await space_for(w.session, other, user)


async def test_data_layer_enforces_visibility(data_world, cipher) -> None:
    from obot.data.errors import CollectionNotFound
    from obot.data.query import Metric, availability, find_records, summarize

    w = data_world
    personal = await collection(w, "spend", CollectionScope.PERSONAL, EXPENSES)
    group = await collection(w, "list", CollectionScope.GROUP, EXPENSES)
    shared = await collection(w, "slots", CollectionScope.SHARED, BOOKINGS)
    private = await w.space(w.alice)
    in_group = await w.space(w.alice, tg_chat_id=-100)
    mine = await add_record(w.session, w.pricing, private, personal, {"amount": 5})

    with pytest.raises(CollectionNotFound):
        await add_record(w.session, w.pricing, in_group, personal, {"amount": 5})
    with pytest.raises(CollectionNotFound):
        await find_records(w.session, in_group, personal)
    with pytest.raises(CollectionNotFound):
        await summarize(w.session, in_group, personal, Metric.COUNT)
    with pytest.raises(CollectionNotFound):
        await get_record(w.session, in_group, personal, mine.id)
    with pytest.raises(CollectionNotFound):
        await update_record(w.session, w.pricing, in_group, personal, mine.id, {"amount": 6})
    with pytest.raises(CollectionNotFound):
        await delete_record(w.session, in_group, personal, mine.id)
    with pytest.raises(CollectionNotFound):
        await add_record(w.session, w.pricing, private, group, {"amount": 5})
    with pytest.raises(CollectionNotFound):
        await availability(w.session, in_group, shared)

    foreign = await other_bot_world(w, cipher)
    with pytest.raises(CollectionNotFound):
        await find_records(w.session, foreign, personal)
