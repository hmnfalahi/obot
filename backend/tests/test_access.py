from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.types import Chat, Message, MessageEntity, User
from sqlalchemy.orm import joinedload

from obot.bots.access import can_use
from obot.bots.users import has_started
from obot.db.models import Bot as BotRecord
from obot.db.models import BotVisibility
from obot.telegram import bots as bots_module
from tests.test_api import ALICE, auth
from tests.test_api import make_bot as make_api_bot
from tests.test_history import make_bot

OWNER = 1001  # make_bot's creator
ALI = User(id=5005, is_bot=False, first_name="Ali", language_code="en")
GROUP = Chat(id=-100, type="supergroup", title="G")


async def private_bot(session, cipher) -> BotRecord:
    record = await session.get(
        BotRecord, await make_bot(session, cipher, 1), options=[joinedload(BotRecord.owner)]
    )
    record.username = "dw_bot"
    return record


async def test_new_bots_are_private_to_their_owner_and_chosen_people(session, cipher) -> None:
    bot = await private_bot(session, cipher)

    assert bot.visibility == BotVisibility.PRIVATE
    assert can_use(bot, OWNER) and not can_use(bot, ALI.id)

    bot.allowed_users = [{"id": ALI.id, "name": "Ali"}]
    assert can_use(bot, ALI.id)

    bot.allowed_users, bot.visibility = [], BotVisibility.PUBLIC
    assert can_use(bot, ALI.id)


def message(text: str, chat: Chat, entities=None) -> Message:
    return Message(
        message_id=10,
        date=datetime.now(UTC),
        chat=chat,
        from_user=ALI,
        text=text,
        entities=entities,
    )


async def test_private_bot_turns_strangers_away_without_a_model_call(
    session, cipher, monkeypatch
) -> None:
    bot = await private_bot(session, cipher)
    answered = []

    async def fake_respond(*args) -> None:
        answered.append(args)

    monkeypatch.setattr(bots_module, "_respond", fake_respond)
    runtime = SimpleNamespace(sender=AsyncMock(), redis=None)
    client = AsyncMock(id=42)

    await bots_module.on_private_text(
        message("hi", Chat(id=ALI.id, type="private")).as_(client), session, bot, runtime
    )
    mention = [MessageEntity(type="mention", offset=0, length=7)]
    await bots_module.on_group_text(
        message("@dw_bot hi", GROUP, mention).as_(client), session, bot, runtime
    )

    assert answered == []
    assert not await has_started(session, bot, ALI.id)
    # One notice, in private; the group hears nothing.
    calls = runtime.sender.send_text.await_args_list
    assert len(calls) == 1 and calls[0].args[1] == ALI.id and "private" in calls[0].args[2]


async def test_owner_shares_a_bot_and_telegram_is_told(api) -> None:
    bot_id = await make_api_bot(api, ALICE, 70)
    async with api.state.sessionmaker() as session:
        record = await session.get(BotRecord, bot_id)
        record.allowed_users = [{"id": 5005, "name": "Ali"}, {"id": 6006, "name": "Sara"}]
        await session.commit()

    detail = (await api.get(f"/api/bots/{bot_id}", headers=auth(ALICE))).json()
    assert detail["visibility"] == "private"
    assert [u["name"] for u in detail["allowed_users"]] == ["Ali", "Sara"]
    assert (detail["users_count"], detail["groups_count"]) == (0, 0)

    kept = await api.patch(
        f"/api/bots/{bot_id}", json={"allowed_user_ids": [6006]}, headers=auth(ALICE)
    )
    public = await api.patch(
        f"/api/bots/{bot_id}", json={"visibility": "public"}, headers=auth(ALICE)
    )

    assert [u["id"] for u in kept.json()["allowed_users"]] == [6006]
    assert public.json()["visibility"] == "public"
    assert api.access_synced == [("private", [6006]), ("public", [6006])]


async def test_access_change_is_not_saved_when_telegram_refuses(api, monkeypatch) -> None:
    from obot.api import routes

    async def refuse(factory, bot) -> bool:
        return False

    monkeypatch.setattr(routes, "sync_access", refuse)
    bot_id = await make_api_bot(api, ALICE, 71)

    response = await api.patch(
        f"/api/bots/{bot_id}", json={"visibility": "public"}, headers=auth(ALICE)
    )

    assert response.status_code == 502
    detail = (await api.get(f"/api/bots/{bot_id}", headers=auth(ALICE))).json()
    assert detail["visibility"] == "private"


async def test_people_chosen_in_the_factory_replace_the_list(session, cipher, monkeypatch) -> None:
    from aiogram.types import SharedUser, UsersShared

    from obot.telegram import factory

    bot = await private_bot(session, cipher)
    bot.allowed_users = [{"id": 9009, "name": "Old"}]
    synced = []

    async def record_access(client, record) -> bool:
        synced.append([u["id"] for u in record.allowed_users])
        return True

    monkeypatch.setattr(factory, "sync_access", record_access)
    owner = User(id=OWNER, is_bot=False, first_name="O", language_code="en")
    shared = UsersShared(
        request_id=bot.id,
        users=[
            SharedUser(user_id=ALI.id, first_name="Ali", last_name="R"),
            SharedUser(user_id=OWNER, first_name="Me"),  # the owner always has access
        ],
    )
    msg = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=OWNER, type="private"),
        from_user=owner,
        users_shared=shared,
    ).as_(AsyncMock())

    await factory.on_people_shared(msg, AsyncMock(), session)

    assert bot.allowed_users == [{"id": ALI.id, "name": "Ali R"}]
    assert synced == [[ALI.id]]
