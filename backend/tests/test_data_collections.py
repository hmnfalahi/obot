from pathlib import Path

import pytest

from obot.billing.plans import Pricing
from obot.bots.service import ManagedBotInfo, register_managed_bot
from obot.data.errors import CollectionNotFound, InvalidSchema, LimitReached, NotAllowed
from obot.data.repository import (
    collection_schema,
    create_collection,
    delete_collection,
    get_collection,
    list_collections,
    update_collection_schema,
)
from obot.data.schema import CollectionSchema
from obot.db.models import Collection, CollectionScope

PLANS_FILE = Path(__file__).parent.parent / "plans.yaml"


def test_plans_have_data_limits() -> None:
    pricing = Pricing.load(PLANS_FILE)
    for name in ("free", "paid"):
        plan = pricing.plan(name)
        assert plan.max_data_collections > 0
        assert plan.max_data_records > 0
        assert plan.max_record_bytes > 0


async def test_collection_table_exists(session, cipher) -> None:
    async def fetch_token(_: int) -> str:
        return "t"

    bot = (
        await register_managed_bot(
            session, cipher, fetch_token, 1001, ManagedBotInfo(1, "b1", "B"), 10
        )
    ).bot
    session.add(
        Collection(
            bot_id=bot.id,
            key="expenses",
            title={"en": "Expenses"},
            scope=CollectionScope.PERSONAL,
            schema={"fields": []},
            rules={},
        )
    )
    await session.flush()


NOTES = CollectionSchema.model_validate(
    {"fields": [{"key": "text", "label": {"en": "Text"}, "type": "text", "required": True}]}
)


async def make(w, key, scope, created_by=None):
    return await create_collection(
        w.session,
        w.pricing,
        bot=w.bot,
        key=key,
        title={"en": key},
        scope=scope,
        schema=NOTES,
        created_by=created_by,
    )


async def test_visibility_by_scope_and_chat(data_world) -> None:
    w = data_world
    await make(w, "notes", CollectionScope.PERSONAL)
    await make(w, "bookings", CollectionScope.SHARED)
    await make(w, "shopping", CollectionScope.GROUP)
    await make(w, "weight", CollectionScope.PERSONAL, created_by=w.alice)

    alice_private = [c.key for c in await list_collections(w.session, await w.space(w.alice))]
    bob_private = [c.key for c in await list_collections(w.session, await w.space(w.bob))]
    alice_group = [
        c.key for c in await list_collections(w.session, await w.space(w.alice, tg_chat_id=-5))
    ]

    assert alice_private == ["notes", "bookings", "weight"]
    assert bob_private == ["notes", "bookings"]
    assert alice_group == ["shopping"]
    with pytest.raises(CollectionNotFound):
        await get_collection(w.session, await w.space(w.bob), "weight")
    with pytest.raises(CollectionNotFound):
        await get_collection(w.session, await w.space(w.alice), "shopping")


async def test_keys_are_unique_per_bot(data_world) -> None:
    w = data_world
    await make(w, "notes", CollectionScope.PERSONAL)
    with pytest.raises(InvalidSchema):
        await make(w, "notes", CollectionScope.SHARED)
    with pytest.raises(InvalidSchema):
        await make(w, "notes", CollectionScope.PERSONAL, created_by=w.alice)
    with pytest.raises(InvalidSchema):
        await make(w, "Bad Key", CollectionScope.PERSONAL)


async def test_only_personal_collections_are_made_in_chat(data_world) -> None:
    with pytest.raises(NotAllowed):
        await make(data_world, "x", CollectionScope.SHARED, created_by=data_world.alice)


async def test_collection_limit(data_world) -> None:
    w = data_world
    limit = w.pricing.plan("free").max_data_collections
    for i in range(limit):
        await make(w, f"c{i}", CollectionScope.PERSONAL)
    with pytest.raises(LimitReached):
        await make(w, "extra", CollectionScope.PERSONAL)
    # A person's own collections are counted separately, against their plan.
    await make(w, "mine", CollectionScope.PERSONAL, created_by=w.alice)


async def test_schema_update_and_delete(data_world) -> None:
    w = data_world
    c = await make(w, "notes", CollectionScope.PERSONAL)
    grown = CollectionSchema.model_validate(
        {
            "fields": [
                *NOTES.model_dump()["fields"],
                {"key": "tag", "label": {"en": "Tag"}, "type": "text"},
            ]
        }
    )
    await update_collection_schema(w.session, c, grown)
    assert collection_schema(c) == grown
    assert c.schema_version == 2

    await delete_collection(w.session, c)
    with pytest.raises(CollectionNotFound):
        await get_collection(w.session, await w.space(w.alice), "notes")


async def test_schema_update_checks_evolution_only_with_records(data_world) -> None:
    w = data_world
    c = await make(w, "notes", CollectionScope.PERSONAL)
    other = CollectionSchema.model_validate(
        {"fields": [{"key": "n", "label": {"en": "N"}, "type": "number"}]}
    )
    await update_collection_schema(w.session, c, other)  # no records yet: anything goes
    assert collection_schema(c) == other
    # Rejection once records exist is covered in test_data_records.py.


async def test_template_hides_a_personal_collection_with_its_key(data_world) -> None:
    w = data_world
    mine = await make(w, "notes", CollectionScope.PERSONAL, created_by=w.alice)
    template = await make(w, "notes", CollectionScope.PERSONAL)
    other = await make(w, "weight", CollectionScope.PERSONAL, created_by=w.alice)
    alice = await w.space(w.alice)

    listed = await list_collections(w.session, alice)
    assert [c.id for c in listed] == [template.id, other.id]
    assert mine not in listed
    assert (await get_collection(w.session, alice, "notes")).id == template.id


async def test_shared_unique_rules_need_public_fields(data_world) -> None:
    w = data_world
    private = CollectionSchema.model_validate(
        {
            "fields": [
                {"key": "name", "label": {"en": "N"}, "type": "text"},
                {"key": "d", "label": {"en": "D"}, "type": "date", "public": True},
            ],
            "rules": {"unique": [["d", "name"]]},
        }
    )
    with pytest.raises(InvalidSchema):
        await create_collection(
            w.session,
            w.pricing,
            bot=w.bot,
            key="slots",
            title={"en": "s"},
            scope=CollectionScope.SHARED,
            schema=private,
        )
    # Fine where nobody else's records are visible.
    await create_collection(
        w.session,
        w.pricing,
        bot=w.bot,
        key="mine",
        title={"en": "m"},
        scope=CollectionScope.PERSONAL,
        schema=private,
    )
    shared = await make(w, "slots", CollectionScope.SHARED)
    with pytest.raises(InvalidSchema):
        await update_collection_schema(w.session, shared, private)
