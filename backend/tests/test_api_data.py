from sqlalchemy import select
from sqlalchemy.orm import joinedload

from obot.data.repository import create_collection
from obot.data.schema import CollectionSchema
from obot.db.models import Bot, BotStatus, BotVisibility, CollectionScope, User
from tests.test_api import ALICE, BOB, auth, make_bot

BOOKINGS = CollectionSchema.model_validate(
    {
        "fields": [
            {"key": "name", "label": {"fa": "نام", "en": "Name"}, "type": "text", "required": True},
            {"key": "date", "label": {"en": "Date"}, "type": "date", "public": True},
            {"key": "time", "label": {"en": "Time"}, "type": "time", "public": True},
            {"key": "secret", "label": {"en": "Old"}, "type": "text", "hidden": True},
        ],
        "rules": {"unique": [["date", "time"]]},
    }
)


async def setup(api, scope=CollectionScope.SHARED, public=True):
    """Alice (1001) owns a bot with a bookings collection; Bob (2002) may use it."""
    bot_id = await make_bot(api, ALICE, 71)
    async with api.state.sessionmaker() as session:
        bot = await session.get(Bot, bot_id, options=[joinedload(Bot.owner)])
        if public:
            bot.visibility = BotVisibility.PUBLIC
        c = await create_collection(
            session,
            api.state.pricing,
            bot=bot,
            key="bookings",
            title={"fa": "نوبت‌ها", "en": "Bookings"},
            scope=scope,
            schema=BOOKINGS,
        )
        await session.commit()
        return bot_id, c.id


def records_url(bot_id, cid, rid=None) -> str:
    base = f"/api/bots/{bot_id}/collections/{cid}/records"
    return base if rid is None else f"{base}/{rid}"


async def test_collections_list_for_owner_and_user(api) -> None:
    bot_id, cid = await setup(api)

    owner = (await api.get(f"/api/bots/{bot_id}/collections", headers=auth(ALICE))).json()
    user = (await api.get(f"/api/bots/{bot_id}/collections", headers=auth(BOB))).json()

    assert [c["key"] for c in owner] == ["bookings"]
    assert owner[0]["can_edit"] is True and user[0]["can_edit"] is False
    assert owner[0]["scope"] == "shared" and owner[0]["timezone"]
    assert [f["key"] for f in owner[0]["fields"]][:3] == ["name", "date", "time"]


async def test_records_crud_with_versions(api) -> None:
    bot_id, cid = await setup(api)

    made = await api.post(
        records_url(bot_id, cid),
        json={"data": {"name": "Bob", "date": "2030-01-10", "time": "17:00"}},
        headers=auth(BOB),
    )
    assert made.status_code == 201
    rec = made.json()
    assert rec["mine"] is True and rec["version"] == 1 and "secret" not in rec["data"]

    changed = await api.patch(
        records_url(bot_id, cid, rec["id"]),
        json={"changes": {"time": "18:00"}, "version": 1},
        headers=auth(BOB),
    )
    assert changed.json()["data"]["time"] == "18:00"
    stale = await api.patch(
        records_url(bot_id, cid, rec["id"]),
        json={"changes": {"time": "19:00"}, "version": 1},
        headers=auth(BOB),
    )
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "stale"

    owner_page = (await api.get(records_url(bot_id, cid), headers=auth(ALICE))).json()
    assert [r["data"]["name"] for r in owner_page["records"]] == ["Bob"]
    assert owner_page["records"][0]["mine"] is False and owner_page["has_more"] is False

    gone = await api.delete(records_url(bot_id, cid, rec["id"]), headers=auth(BOB))
    assert gone.status_code == 204
    assert (await api.get(records_url(bot_id, cid), headers=auth(BOB))).json()["records"] == []


async def test_errors_are_structured(api) -> None:
    bot_id, cid = await setup(api)
    body = {"data": {"name": "A", "date": "2030-01-10", "time": "17:00"}}
    await api.post(records_url(bot_id, cid), json=body, headers=auth(BOB))

    dup = await api.post(records_url(bot_id, cid), json=body, headers=auth(ALICE))
    bad = await api.post(
        records_url(bot_id, cid), json={"data": {"date": "soon"}}, headers=auth(BOB)
    )

    assert dup.status_code == 422 and dup.json()["detail"]["code"] == "duplicate"
    assert dup.json()["detail"]["fields"] == ["date", "time"]
    detail = bad.json()["detail"]
    assert detail["code"] == "invalid"
    assert {e["field"] for e in detail["errors"]} == {"date", "name"}


async def test_strangers_and_other_peoples_records(api) -> None:
    bot_id, cid = await setup(api, public=False)  # private bot: Bob may not use it
    assert (await api.get(f"/api/bots/{bot_id}/collections", headers=auth(BOB))).status_code == 404

    bot_id2, cid2 = await setup_second(api)
    alice_rec = await api.post(
        records_url(bot_id2, cid2),
        json={"data": {"name": "A", "date": "2030-01-10", "time": "17:00"}},
        headers=auth(ALICE),
    )
    # Bob cannot read, change or delete the owner's record in a shared collection.
    rid = alice_rec.json()["id"]
    assert (
        await api.patch(
            records_url(bot_id2, cid2, rid), json={"changes": {"name": "X"}}, headers=auth(BOB)
        )
    ).status_code == 404
    assert (await api.delete(records_url(bot_id2, cid2, rid), headers=auth(BOB))).status_code == 404


async def setup_second(api):
    bot_id = await make_bot(api, ALICE, 72)
    async with api.state.sessionmaker() as session:
        bot = await session.get(Bot, bot_id, options=[joinedload(Bot.owner)])
        bot.visibility = BotVisibility.PUBLIC
        c = await create_collection(
            session,
            api.state.pricing,
            bot=bot,
            key="bookings",
            title={"en": "B"},
            scope=CollectionScope.SHARED,
            schema=BOOKINGS,
        )
        await session.commit()
        return bot_id, c.id


async def test_search_filters_and_paging(api) -> None:
    bot_id, cid = await setup(api)
    for i, name in enumerate(["Sara", "Ali", "Sarah"]):
        await api.post(
            records_url(bot_id, cid),
            json={"data": {"name": name, "date": "2030-01-10", "time": f"1{i}:00"}},
            headers=auth(ALICE),
        )

    found = (await api.get(records_url(bot_id, cid) + "?q=sar", headers=auth(ALICE))).json()
    assert sorted(r["data"]["name"] for r in found["records"]) == ["Sara", "Sarah"]
    paged = (
        await api.get(
            records_url(bot_id, cid) + "?sort=time&desc=false&limit=2", headers=auth(ALICE)
        )
    ).json()
    assert [r["data"]["name"] for r in paged["records"]] == ["Sara", "Ali"]
    assert paged["has_more"] is True
    filtered = (
        await api.get(
            records_url(bot_id, cid) + '?filters=[{"field":"time","op":"gte","value":"11:00"}]',
            headers=auth(ALICE),
        )
    ).json()
    assert len(filtered["records"]) == 2
    broken = await api.get(records_url(bot_id, cid) + "?filters=nope", headers=auth(ALICE))
    assert broken.status_code == 422


async def test_malformed_filters_are_clear_422s(api) -> None:
    bot_id, cid = await setup(api)
    for bad in ("5", "true", "nope", '{"a":1}', "[1]"):
        r = await api.get(records_url(bot_id, cid) + f"?filters={bad}", headers=auth(ALICE))
        assert r.status_code == 422, bad
        assert r.json()["detail"]["code"] == "invalid_query"
        assert {"message", "errors", "fields"} <= set(r.json()["detail"])


async def test_offset_paging_and_bound(api) -> None:
    bot_id, cid = await setup(api)
    for i in range(3):
        await api.post(
            records_url(bot_id, cid),
            json={"data": {"name": f"N{i}", "date": "2030-01-10", "time": f"1{i}:00"}},
            headers=auth(ALICE),
        )
    url = records_url(bot_id, cid) + "?sort=time&desc=false&limit=2&offset=2"
    page = (await api.get(url, headers=auth(ALICE))).json()
    assert [r["data"]["name"] for r in page["records"]] == ["N2"]
    assert page["has_more"] is False
    too_far = await api.get(records_url(bot_id, cid) + "?offset=100001", headers=auth(ALICE))
    assert too_far.status_code == 422


async def test_hidden_fields_only_for_editors(api) -> None:
    bot_id, cid = await setup(api)
    owner = (await api.get(f"/api/bots/{bot_id}/collections", headers=auth(ALICE))).json()
    user = (await api.get(f"/api/bots/{bot_id}/collections", headers=auth(BOB))).json()
    assert "secret" in [f["key"] for f in owner[0]["fields"]]
    assert "secret" not in [f["key"] for f in user[0]["fields"]]


async def test_personal_collection_over_http(api) -> None:
    bot_id = await make_bot(api, ALICE, 73)
    async with api.state.sessionmaker() as session:
        bot = await session.get(Bot, bot_id, options=[joinedload(Bot.owner)])
        bot.visibility = BotVisibility.PUBLIC
        await session.commit()
    # Bob's first call creates his user row; a personal collection is made by him in chat.
    await api.get(f"/api/bots/{bot_id}/collections", headers=auth(BOB))
    async with api.state.sessionmaker() as session:
        bot = await session.get(Bot, bot_id, options=[joinedload(Bot.owner)])
        bob = await session.scalar(select(User).where(User.tg_user_id == BOB["id"]))
        c = await create_collection(
            session,
            api.state.pricing,
            bot=bot,
            key="diary",
            title={"en": "Diary"},
            scope=CollectionScope.PERSONAL,
            schema=BOOKINGS,
            created_by=bob,
        )
        await session.commit()
        cid = c.id
    body = {"data": {"name": "Mine", "date": "2030-01-10", "time": "17:00"}}
    assert (
        await api.post(records_url(bot_id, cid), json=body, headers=auth(BOB))
    ).status_code == 201
    bobs = (await api.get(records_url(bot_id, cid), headers=auth(BOB))).json()
    assert [r["data"]["name"] for r in bobs["records"]] == ["Mine"]
    assert (await api.get(records_url(bot_id, cid), headers=auth(ALICE))).status_code == 404


async def test_inactive_bot_is_404(api) -> None:
    bot_id, cid = await setup(api)
    async with api.state.sessionmaker() as session:
        bot = await session.get(Bot, bot_id)
        bot.status = BotStatus.SUSPENDED
        await session.commit()
    for who in (ALICE, BOB):
        r = await api.get(f"/api/bots/{bot_id}/collections", headers=auth(who))
        assert r.status_code == 404
