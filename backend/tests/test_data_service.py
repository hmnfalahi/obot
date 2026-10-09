from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from obot.data.format import describe_group_only
from obot.data.query import find_records
from obot.data.repository import (
    collection_schema,
    create_collection,
    get_collection,
    update_collection_schema,
)
from obot.data.schema import CollectionSchema
from obot.data.service import DataService, Definition, GroupChat, apply_definition
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


async def _hide_category(world) -> None:
    async with world.sessions() as s:
        space = await world.space(world.alice)
        c = await get_collection(s, space, "expenses")
        schema = collection_schema(c).model_dump(mode="json")
        for f in schema["fields"]:
            if f["key"] == "category":
                f["hidden"] = True
        await update_collection_schema(s, c, CollectionSchema.model_validate(schema))
        await s.commit()


async def test_ok_texts_omit_hidden_fields(world) -> None:
    alice = service(world, world.alice)
    saved = await alice.add("expenses", {"amount": 3, "category": "food"})
    record_id = int(saved.split("#")[1].split(":")[0])
    await _hide_category(world)
    updated = await alice.update("expenses", record_id, {"amount": 4})
    assert updated.startswith("OK:") and '"amount": "4"' in updated and "food" not in updated
    added = await alice.add("expenses", {"amount": 5})
    assert "category" not in added


async def test_failing_notice_does_not_undo_the_save(world) -> None:
    notice = AsyncMock(side_effect=RuntimeError("telegram down"))
    bob = service(world, world.bob, notice_shared=notice)
    ok = await bob.add("bookings", {"name": "Bob", "date": "2026-10-11", "time": "10:00"})
    assert ok.startswith("OK:") and notice.await_count == 1
    owner = service(world, world.owner)
    assert (await owner.find("bookings")).count("#") == 1


async def test_bad_inputs_never_raise(world) -> None:
    alice = service(world, world.alice)
    for bad in (0, 10**30, -5):
        assert "no record" in await alice.update("expenses", bad, {"amount": 1})
        confirm = AsyncMock()
        assert "no record" in await service(world, world.alice, confirm_delete=confirm).delete(
            "expenses", bad
        )
        confirm.assert_not_awaited()
    assert (await alice.add("expenses", ["x"])).startswith("ERROR: ")  # type: ignore[arg-type]
    assert (await alice.update("expenses", 1, "x")).startswith("ERROR: ")  # type: ignore[arg-type]


async def test_raising_confirm_is_reported_as_text(world) -> None:
    saved = await service(world, world.alice).add("expenses", {"amount": 7})
    record_id = int(saved.split("#")[1].split(":")[0])
    confirm = AsyncMock(side_effect=RuntimeError("boom"))
    out = await service(world, world.alice, confirm_delete=confirm).delete("expenses", record_id)
    assert out == "ERROR: could not ask the user to confirm; try again later."


async def test_missing_user_is_collection_not_found(world) -> None:
    out = await DataService(world.sessions, world.pricing, world.bot.id, 10**9).find("expenses")
    assert "no collection named 'expenses'" in out


async def test_unexpected_failures_become_error_text(world) -> None:
    from sqlalchemy.exc import TimeoutError as PoolTimeout

    def broken(exc):
        def factory():
            raise exc

        return factory

    for exc in (PoolTimeout("pool exhausted"), RuntimeError("boom")):
        failing = DataService(broken(exc), world.pricing, world.bot.id, world.alice.id)
        assert (await failing.find("expenses")).startswith("ERROR:")
        assert (await failing.add("expenses", {"amount": 1})).startswith("ERROR:")


async def test_catalog_failure_gives_no_note(world) -> None:
    class Broken:
        async def get(self, *args):
            raise RuntimeError("down")

    assert await service(world, world.alice).note(Broken()) is None


async def test_note_with_the_switch_and_no_collections(data_world) -> None:
    w = data_world
    await w.session.commit()
    sessions = async_sessionmaker(w.session.bind, expire_on_commit=False)
    service = DataService(sessions, w.pricing, w.bot.id, w.alice.id)
    async with sessions() as s:
        assert await service.note(s) is None
        note = await service.note(s, can_define=True)
    assert "no data collections yet" in note


async def test_a_failing_catalog_leaves_the_callers_session_usable(data_world, monkeypatch) -> None:
    from sqlalchemy import text

    from obot.data import service as service_module

    w = data_world

    async def broken(session, space):
        await session.execute(text("select * from no_such_table"))

    monkeypatch.setattr(service_module, "list_collections", broken)
    service = DataService(None, w.pricing, w.bot.id, w.alice.id)

    assert await service.note(w.session) is None
    # The failed statement was rolled back to a savepoint: the reply can go on.
    assert await w.session.scalar(text("select 1")) == 1


WEIGHT_FIELDS = [
    {"key": "day", "label": "روز", "type": "date", "required": True},
    {"key": "kg", "label": "وزن", "type": "number", "required": True},
]


async def test_define_proposes_and_creates_nothing_until_applied(data_world) -> None:
    w = data_world
    await w.session.commit()
    sessions = async_sessionmaker(w.session.bind, expire_on_commit=False)
    propose = AsyncMock(return_value="Shown with Save/Cancel.")
    alice = DataService(sessions, w.pricing, w.bot.id, w.alice.id, propose_define=propose)

    told = await alice.define("weight", "وزن من", WEIGHT_FIELDS)

    assert told == "Shown with Save/Cancel."
    d: Definition = propose.await_args.args[0]
    assert (d.key, d.collection_id, d.added) == ("weight", None, ["day", "kg"])
    assert d.title == {"fa": "وزن من"}  # Alice speaks Persian
    assert d.schema["fields"][1]["label"] == {"fa": "وزن"}
    assert Definition.from_json(d.to_json()) == d
    async with sessions() as s:
        assert await alice.note(s) is None  # nothing created yet

        created = await apply_definition(s, w.pricing, d)
        await s.commit()
    assert created.created_by_user_id == w.alice.id
    assert (await alice.add("weight", {"day": "2026-10-09", "kg": "72.5"})).startswith("OK:")
    # Bob does not see Alice's collection.
    assert "no collection named 'weight'" in await DataService(
        sessions, w.pricing, w.bot.id, w.bob.id
    ).find("weight")


async def test_define_adds_fields_to_the_persons_own_collection(data_world) -> None:
    w = data_world
    await w.session.commit()
    sessions = async_sessionmaker(w.session.bind, expire_on_commit=False)
    propose = AsyncMock(return_value="ok")
    alice = DataService(sessions, w.pricing, w.bot.id, w.alice.id, propose_define=propose)
    await alice.define("weight", "Weight", WEIGHT_FIELDS)
    async with sessions() as s:
        await apply_definition(s, w.pricing, propose.await_args.args[0])
        await s.commit()
    await alice.add("weight", {"day": "2026-10-09", "kg": 72})

    await alice.define(
        "weight",
        "ignored",
        [*WEIGHT_FIELDS, {"key": "note", "label": "Note", "type": "text"}],
    )

    d = propose.await_args.args[0]
    assert d.collection_id is not None and d.added == ["note"]
    async with sessions() as s:
        await apply_definition(s, w.pricing, d)
        await s.commit()
    assert (await alice.add("weight", {"day": "2026-10-10", "kg": 71, "note": "gym"})).startswith(
        "OK:"
    )


async def test_define_refusals_are_error_text(data_world) -> None:
    w = data_world
    await create_collection(
        w.session,
        w.pricing,
        bot=w.bot,
        key="expenses",
        title={"en": "E"},
        scope=CollectionScope.PERSONAL,
        schema=EXPENSES,
    )
    await w.session.commit()
    sessions = async_sessionmaker(w.session.bind, expire_on_commit=False)
    propose = AsyncMock(return_value="ok")
    alice = DataService(sessions, w.pricing, w.bot.id, w.alice.id, propose_define=propose)

    cases = {
        "template": await alice.define("expenses", "E", WEIGHT_FIELDS),
        "bad type": await alice.define("w", "W", [{"key": "a", "label": "A", "type": "color"}]),
        "choice": await alice.define("w", "W", [{"key": "a", "label": "A", "type": "choice"}]),
        "bad key": await alice.define("My List", "W", WEIGHT_FIELDS),
        "no fields": await alice.define("w", "W", []),
        "not a list": await alice.define("w", "W", "day, kg"),
    }
    for name, text in cases.items():
        assert text.startswith("ERROR: "), (name, text)
    assert "bot's own collections" in cases["template"]
    propose.assert_not_awaited()
    assert (
        await DataService(sessions, w.pricing, w.bot.id, w.alice.id).define("w", "W", WEIGHT_FIELDS)
    ).startswith("ERROR: ")


async def test_define_respects_the_persons_collection_limit(data_world) -> None:
    w = data_world
    await w.session.commit()
    w.pricing.plan("free").max_data_collections = 1
    sessions = async_sessionmaker(w.session.bind, expire_on_commit=False)
    propose = AsyncMock(return_value="ok")
    alice = DataService(sessions, w.pricing, w.bot.id, w.alice.id, propose_define=propose)
    await alice.define("weight", "W", WEIGHT_FIELDS)
    async with sessions() as s:
        await apply_definition(s, w.pricing, propose.await_args.args[0])
        await s.commit()

    assert "limit" in await alice.define("sleep", "S", WEIGHT_FIELDS)


async def test_nothing_new_to_add_is_refused(data_world) -> None:
    w = data_world
    await w.session.commit()
    sessions = async_sessionmaker(w.session.bind, expire_on_commit=False)
    propose = AsyncMock(return_value="ok")
    alice = DataService(sessions, w.pricing, w.bot.id, w.alice.id, propose_define=propose)
    await alice.define("weight", "W", WEIGHT_FIELDS)
    async with sessions() as s:
        await apply_definition(s, w.pricing, propose.await_args.args[0])
        await s.commit()

    assert "nothing new" in await alice.define("weight", "W", WEIGHT_FIELDS)


async def _weight(w):
    await w.session.commit()
    sessions = async_sessionmaker(w.session.bind, expire_on_commit=False)
    propose = AsyncMock(return_value="ok")
    alice = DataService(sessions, w.pricing, w.bot.id, w.alice.id, propose_define=propose)
    await alice.define("weight", "W", WEIGHT_FIELDS)
    async with sessions() as s:
        await apply_definition(s, w.pricing, propose.await_args.args[0])
        await s.commit()
    return sessions, propose, alice


@pytest.mark.parametrize("with_records", [True, False])
async def test_stale_proposal_is_refused(data_world, with_records) -> None:
    from obot.data.errors import DataError

    w = data_world
    sessions, propose, alice = await _weight(w)
    if with_records:
        await alice.add("weight", {"day": "2026-10-09", "kg": 72})
    await alice.define(
        "weight", "W", [*WEIGHT_FIELDS, {"key": "note", "label": "N", "type": "text"}]
    )
    p1 = propose.await_args.args[0]
    await alice.define(
        "weight", "W", [*WEIGHT_FIELDS, {"key": "mood", "label": "M", "type": "text"}]
    )
    p2 = propose.await_args.args[0]
    async with sessions() as s:
        await apply_definition(s, w.pricing, p2)
        await s.commit()
    async with sessions() as s:
        with pytest.raises(DataError, match="changed since"):
            await apply_definition(s, w.pricing, p1)
        await s.rollback()
        c = await get_collection(s, await _space(s, w), "weight")
        assert "mood" in [f.key for f in collection_schema(c).fields]


async def _space(s, w):
    from obot.data.space import space_for
    from obot.db.models import Bot, User

    return await space_for(s, await s.get(Bot, w.bot.id), await s.get(User, w.alice.id))


async def test_define_rejects_bad_options_types_and_hidden_fields(data_world) -> None:
    w = data_world
    sessions, propose, alice = await _weight(w)
    await alice.define(
        "weight",
        "W",
        [*WEIGHT_FIELDS, {"key": "tag", "label": "T", "type": "choice", "options": ["a"]}],
    )
    async with sessions() as s:
        await apply_definition(s, w.pricing, propose.await_args.args[0])
        await s.commit()
    propose.reset_mock()
    tag = {"key": "tag", "label": "T", "type": "choice"}
    out = [
        await alice.define("weight", "W", [*WEIGHT_FIELDS, {**tag, "options": "gym"}]),
        await alice.define("weight", "W", [*WEIGHT_FIELDS, {**tag, "type": "text"}]),
    ]
    assert all(t.startswith("ERROR: ") for t in out), out
    assert "already exists as choice" in out[1]
    # Hide the field, then try to bring it back or add options to it.
    async with sessions() as s:
        c = await get_collection(s, await _space(s, w), "weight")
        data = collection_schema(c).model_dump(mode="json")
        for f in data["fields"]:
            if f["key"] == "tag":
                f["hidden"] = True
        await update_collection_schema(s, c, CollectionSchema.model_validate(data))
        await s.commit()
    for spec in (tag, {**tag, "options": ["b"]}):
        text = await alice.define("weight", "W", [*WEIGHT_FIELDS, spec])
        assert "can't be added back" in text, text
    propose.assert_not_awaited()


async def test_template_with_same_key_hides_the_proposal_target(data_world) -> None:
    from obot.data.errors import DataError

    w = data_world
    sessions, propose, alice = await _weight(w)
    await alice.define(
        "weight", "W", [*WEIGHT_FIELDS, {"key": "note", "label": "N", "type": "text"}]
    )
    p = propose.await_args.args[0]
    async with sessions() as s:
        await create_collection(
            s,
            w.pricing,
            bot=w.bot,
            key="weight",
            title={"en": "T"},
            scope=CollectionScope.PERSONAL,
            schema=EXPENSES,
        )
        await s.commit()
    async with sessions() as s:
        with pytest.raises(DataError, match="no longer available"):
            await apply_definition(s, w.pricing, p)


SHOPPING = CollectionSchema.model_validate(
    {"fields": [{"key": "item", "label": {"en": "Item"}, "type": "text", "required": True}]}
)


@pytest.fixture
async def family(world):
    w = world
    await create_collection(
        w.session,
        w.pricing,
        bot=w.bot,
        key="shopping",
        title={"en": "Shopping"},
        scope=CollectionScope.GROUP,
        schema=SHOPPING,
    )
    await w.session.commit()
    return w


def in_group(w, user, chat_id=-100, admin=False, **kwargs):
    asked = AsyncMock(return_value=admin)
    s = service(w, user, group=GroupChat(chat_id, asked, title="Family"), **kwargs)
    return s, asked


def record_id(text: str) -> int:
    return int(text.split("#")[1].split(":")[0])


async def test_a_group_sees_only_its_group_collections(family) -> None:
    w = family
    alice, _ = in_group(w, w.alice)
    note = await alice.note(w.session)
    assert "shopping" in note and "expenses" not in note and "bookings" not in note
    assert "admin" in note  # the model knows who may change what
    private_note = await service(w, w.alice).note(w.session)
    assert "shopping" not in private_note

    assert (await alice.add("shopping", {"item": "milk"})).startswith("OK")
    assert "no collection" in await alice.add("expenses", {"amount": 1})
    assert "no collection" in await service(w, w.alice).find("shopping")


async def test_group_x_does_not_see_group_y(family) -> None:
    w = family
    x, _ = in_group(w, w.alice, chat_id=-100)
    y, _ = in_group(w, w.alice, chat_id=-200)
    await x.add("shopping", {"item": "milk"})
    assert "milk" in await x.find("shopping")
    assert "milk" not in await y.find("shopping")
    bob_x, _ = in_group(w, w.bob, chat_id=-100)
    assert "milk" in await bob_x.find("shopping")  # every member reads the group's list


async def test_members_change_their_own_items_and_admins_any(family) -> None:
    w = family
    alice, alice_asked = in_group(w, w.alice)
    saved = await alice.add("shopping", {"item": "milk"})
    await alice.find("shopping")
    alice_asked.assert_not_awaited()  # adding and reading never ask Telegram
    rid = record_id(saved)

    bob, _ = in_group(w, w.bob)
    refused = await bob.update("shopping", rid, {"item": "bread"})
    assert refused.startswith("ERROR") and "group admin" in refused

    admin, admin_asked = in_group(w, w.bob, admin=True)
    assert (await admin.update("shopping", rid, {"item": "bread"})).startswith("OK")
    admin_asked.assert_awaited()


async def test_group_delete_goes_to_confirmation_for_author_or_admin(family) -> None:
    w = family
    confirm = AsyncMock(return_value="asked")
    alice, _ = in_group(w, w.alice, confirm_delete=confirm)
    rid = record_id(await alice.add("shopping", {"item": "milk"}))

    bob, _ = in_group(w, w.bob, confirm_delete=confirm)
    assert (await bob.delete("shopping", rid)).startswith("ERROR")
    confirm.assert_not_awaited()

    admin, _ = in_group(w, w.bob, admin=True, confirm_delete=confirm)
    assert await admin.delete("shopping", rid) == "asked"
    assert await alice.delete("shopping", rid) == "asked"


async def test_private_chats_only_name_the_group_collections(family) -> None:
    w = family
    hint = await service(w, w.alice).group_hint(w.session)
    assert "Shopping" in hint and "group" in hint
    alice, _ = in_group(w, w.alice)
    assert await alice.group_hint(w.session) is None


async def test_no_group_hint_without_group_collections(world) -> None:
    assert await service(world, world.alice).group_hint(world.session) is None
    assert describe_group_only([]) is None


async def test_define_in_a_group_proposes_nothing(data_world) -> None:
    w = data_world
    await w.session.commit()
    sessions = async_sessionmaker(w.session.bind, expire_on_commit=False)
    propose = AsyncMock()
    alice = DataService(
        sessions,
        w.pricing,
        w.bot.id,
        w.alice.id,
        propose_define=propose,
        group=GroupChat(-100, AsyncMock(return_value=False)),
    )

    told = await alice.define("weight", "Weight", WEIGHT_FIELDS)

    assert told.startswith("ERROR")
    propose.assert_not_awaited()
