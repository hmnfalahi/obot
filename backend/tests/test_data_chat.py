import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiogram.methods import AnswerCallbackQuery, EditMessageText
from aiogram.types import CallbackQuery, Chat, Message
from aiogram.types import User as TgUser
from sqlalchemy.ext.asyncio import async_sessionmaker

from obot.bots.users import join_bot
from obot.core.tools import (
    DATA_TOOLS,
    DEFINE_TOOL,
    ToolServices,
    build_tools,
    data_add,
    data_define,
)
from obot.data.format import DEFINE_HINT
from obot.data.query import find_records
from obot.data.repository import MAX_ID, create_collection, get_collection
from obot.data.schema import CollectionSchema
from obot.data.service import DataService, GroupChat
from obot.db.models import BotVisibility, CollectionScope, Record
from obot.i18n import t
from obot.telegram import data_chat
from obot.telegram.data_chat import DefineProposer, DeleteConfirmer, SharedNotice
from obot.telegram.group_access import GroupRole
from obot.telegram.toolkit import toolkit

EXPENSES = CollectionSchema.model_validate(
    {"fields": [{"key": "amount", "label": {"en": "Amount"}, "type": "money", "required": True}]}
)
ALICE = TgUser(id=2002, is_bot=False, first_name="Alice", language_code="fa")
BOB = TgUser(id=3003, is_bot=False, first_name="Bob", language_code="en")


def runtime_for(w, redis_set=True):
    client = AsyncMock()
    return SimpleNamespace(
        sessionmaker=async_sessionmaker(w.session.bind, expire_on_commit=False),
        pricing=w.pricing,
        sender=AsyncMock(),
        redis=SimpleNamespace(set=AsyncMock(return_value=redis_set)),
        transport=SimpleNamespace(client=MagicMock(return_value=client)),
        http=None,
        cipher=None,
        tavily_api_key=None,
    )


async def expenses(w):
    c = await create_collection(
        w.session,
        w.pricing,
        bot=w.bot,
        key="expenses",
        title={"en": "Expenses"},
        scope=CollectionScope.PERSONAL,
        schema=EXPENSES,
    )
    await w.session.commit()
    return c


def test_build_tools_adds_data_tools_only_when_asked() -> None:
    assert not {t.name for t in build_tools([])} & set(DATA_TOOLS)
    assert set(DATA_TOOLS) <= {t.name for t in build_tools([], data=True)}


async def test_data_tools_need_a_data_service() -> None:
    ctx = SimpleNamespace(deps=ToolServices(http=None, meter=None))
    assert (await data_add(ctx, "expenses", {"amount": 1})).startswith("ERROR:")


async def test_toolkit_adds_data_tools_and_catalog_unless_data_is_off(data_world) -> None:
    w = data_world
    runtime = runtime_for(w)
    empty = await toolkit(w.session, runtime, w.bot, datetime.now(UTC), w.alice)
    assert not {t.name for t in empty.tools} & set(DATA_TOOLS)

    await expenses(w)
    kit = await toolkit(w.session, runtime, w.bot, datetime.now(UTC), w.alice)
    in_group = await toolkit(w.session, runtime, w.bot, datetime.now(UTC), w.alice, data=False)

    assert set(DATA_TOOLS) <= {t.name for t in kit.tools}
    assert 'expenses "Expenses"' in kit.note
    assert isinstance(kit.services.data, DataService)
    assert not {t.name for t in in_group.tools} & set(DATA_TOOLS)
    assert in_group.services.data is None


async def test_shared_notice_is_sent_once(data_world) -> None:
    w = data_world
    runtime = runtime_for(w)
    await SharedNotice(runtime, w.bot, w.alice)()
    runtime.redis.set = AsyncMock(return_value=False)
    await SharedNotice(runtime, w.bot, w.alice)()

    runtime.sender.send_text.assert_awaited_once()
    call = runtime.sender.send_text.await_args
    assert call.args[1] == ALICE.id and "صاحب" in call.args[2]


async def press(w, runtime, data: str, who: TgUser) -> AsyncMock:
    client = AsyncMock()
    message = Message(
        message_id=3, date=datetime.now(UTC), chat=Chat(id=who.id, type="private"), text="card"
    ).as_(client)
    query = CallbackQuery(id="q", from_user=who, chat_instance="c", data=data, message=message).as_(
        client
    )
    await data_chat.on_data_button(query, w.session, w.bot, runtime)
    return client


async def test_delete_card_deletes_only_for_its_author(data_world) -> None:
    w = data_world
    w.bot.visibility = BotVisibility.PUBLIC  # Alice and Bob may use it
    runtime = runtime_for(w)
    await expenses(w)
    service = DataService(
        runtime.sessionmaker,
        w.pricing,
        w.bot.id,
        w.alice.id,
        confirm_delete=DeleteConfirmer(runtime, w.bot, w.alice),
    )
    saved = await service.add("expenses", {"amount": 7})
    record_id = int(saved.split("#")[1].split(":")[0])

    told = await service.delete("expenses", record_id)

    assert "confirm" in told.lower()
    call = runtime.sender.send_text.await_args
    assert call.args[1] == ALICE.id and "7" in call.args[2]
    keyboard = call.kwargs["buttons"].inline_keyboard[0]
    delete, keep = keyboard[0].callback_data, keyboard[1].callback_data

    await press(w, runtime, keep, ALICE)
    stranger = await press(w, runtime, delete, BOB)
    alert = stranger.await_args.args[0]  # aiogram sends methods by calling the bot
    assert isinstance(alert, AnswerCallbackQuery) and alert.show_alert

    async def remaining() -> list[int]:
        async with runtime.sessionmaker() as s:
            space = await w.space(w.alice)
            c = await get_collection(s, space, "expenses")
            return [r.id for r in await find_records(s, space, c)]

    assert await remaining() == [record_id]
    await press(w, runtime, delete, ALICE)
    assert await remaining() == []


async def shared_notes(w):
    c = await create_collection(
        w.session,
        w.pricing,
        bot=w.bot,
        key="notes",
        title={"en": "Notes"},
        scope=CollectionScope.SHARED,
        schema=CollectionSchema.model_validate(
            {
                "fields": [
                    {"key": "text", "label": {"en": "Text"}, "type": "text", "required": True},
                    {"key": "at", "label": {"en": "At"}, "type": "datetime"},
                    {"key": "secret", "label": {"en": "Secret"}, "type": "text", "hidden": True},
                ]
            }
        ),
    )
    await w.session.commit()
    return c


async def remaining_notes(w, runtime, who) -> list[int]:
    async with runtime.sessionmaker() as s:
        space = await w.space(w.owner)
        c = await get_collection(s, space, "notes")
        return [r.id for r in await find_records(s, space, c)]


async def test_shared_delete_is_for_the_author_or_the_owner(data_world) -> None:
    w = data_world
    w.bot.visibility = BotVisibility.PUBLIC
    runtime = runtime_for(w)
    await shared_notes(w)
    await join_bot(w.session, w.bot, 1001, "en")  # the owner uses the bot too
    await w.session.commit()
    alice = DataService(runtime.sessionmaker, w.pricing, w.bot.id, w.alice.id)
    saved = await alice.add("notes", {"text": "hi", "at": "2026-01-01T12:00"})
    record_id = int(saved.split("#")[1].split(":")[0])
    async with runtime.sessionmaker() as s:  # hidden fields are never set by the model
        record = await s.get(Record, record_id)
        record.data = {**record.data, "secret": "hunter2"}
        await s.commit()

    # the owner is asked to confirm someone else's record
    owner_service = DataService(
        runtime.sessionmaker,
        w.pricing,
        w.bot.id,
        w.owner.id,
        confirm_delete=DeleteConfirmer(runtime, w.bot, w.owner),
    )
    await owner_service.delete("notes", record_id)
    call = runtime.sender.send_text.await_args
    assert call.args[1] == w.owner.tg_user_id
    assert "hi" in call.args[2] and "hunter2" not in call.args[2]
    assert "2026-01-01 12:00" in call.args[2]  # in the owner's zone, not the stored UTC
    delete = call.kwargs["buttons"].inline_keyboard[0][0].callback_data

    stranger = await press(w, runtime, delete, BOB)
    assert isinstance(stranger.await_args.args[0], AnswerCallbackQuery)
    assert stranger.await_args.args[0].show_alert
    assert await remaining_notes(w, runtime, w.owner) == [record_id]

    await press(w, runtime, delete, TgUser(id=1001, is_bot=False, first_name="O"))
    assert await remaining_notes(w, runtime, w.owner) == []

    again = await press(w, runtime, delete, TgUser(id=1001, is_bot=False, first_name="O"))
    answers = [c.args[0] for c in again.await_args_list]
    assert not any(isinstance(a, AnswerCallbackQuery) and a.show_alert for a in answers)


async def test_non_owner_save_on_shared_collection_notifies_once(data_world) -> None:
    w = data_world
    runtime = runtime_for(w)
    await shared_notes(w)
    service = DataService(
        runtime.sessionmaker,
        w.pricing,
        w.bot.id,
        w.alice.id,
        notice_shared=SharedNotice(runtime, w.bot, w.alice),
    )
    await service.add("notes", {"text": "one"})
    runtime.redis.set = AsyncMock(return_value=False)
    await service.add("notes", {"text": "two"})

    runtime.sender.send_text.assert_awaited_once()
    assert runtime.sender.send_text.await_args.args[1] == ALICE.id


async def test_data_off_reaches_the_delegator(data_world, monkeypatch) -> None:
    from obot.telegram import toolkit as toolkit_module

    w = data_world
    runtime = runtime_for(w)
    peer = SimpleNamespace(username="peer", display_name="Peer", persona=None)
    monkeypatch.setattr(toolkit_module, "user_bots", AsyncMock(return_value=[peer]))
    seen = {}

    class Spy:
        def __init__(self, runtime, bot, payer, **kwargs) -> None:
            seen.update(kwargs)

    monkeypatch.setattr(toolkit_module, "Delegator", Spy)
    await toolkit(w.session, runtime, w.bot, datetime.now(UTC), w.alice, data=False)
    assert seen == {"data": False}


def test_tasks_to_groups_get_no_data() -> None:
    from obot.telegram.task_runner import delivers_to_group

    assert delivers_to_group(SimpleNamespace(tg_chat_id=-100123))
    assert not delivers_to_group(SimpleNamespace(tg_chat_id=2002))


async def test_run_step_uses_the_groups_data_for_the_tasks_own_bot(data_world, monkeypatch) -> None:
    from obot.chat.membership import upsert_membership
    from obot.telegram import task_runner

    w = data_world
    await upsert_membership(
        w.session,
        w.bot.id,
        -100,
        chat_type="supergroup",
        title="Family",
        is_member=True,
        is_admin=True,
    )
    await upsert_membership(
        w.session, w.bot.id, -300, chat_type="channel", title="News", is_member=True, is_admin=True
    )

    class Stop(Exception):
        pass

    seen = {}

    async def fake_toolkit(*args, **kwargs):
        seen.clear()
        seen.update(kwargs)
        raise Stop

    monkeypatch.setattr(task_runner, "toolkit", fake_toolkit)
    monkeypatch.setattr(task_runner, "reply_model", AsyncMock(return_value=None))
    runtime = SimpleNamespace(pricing=None, models=None, cipher=None)

    async def run(chat_id: int, own: bool) -> dict:
        scope = SimpleNamespace(tg_chat_id=chat_id, bot_id=w.bot.id)
        try:
            await task_runner._run_step(
                w.session, runtime, w.bot, w.alice, own, "p", None, scope, "q"
            )
        except Stop:
            pass
        return dict(seen)

    own_group = await run(-100, True)
    assert own_group["data"] is True and own_group["group"].tg_chat_id == -100
    assert await own_group["group"].is_admin() is True  # _cannot_run checked it
    assert own_group["group"].title == "Family"
    other_bot = await run(-100, False)
    assert other_bot["data"] is False and other_bot.get("group") is None
    channel = await run(-300, True)
    assert channel["data"] is False and channel.get("group") is None
    private = await run(2002, True)
    assert private["data"] is True and private.get("group") is None


def alerted(client) -> bool:
    answers = [c.args[0] for c in client.await_args_list]
    return any(isinstance(a, AnswerCallbackQuery) and a.show_alert for a in answers)


async def test_already_deleted_card_answers_by_read_and_change_rules(data_world) -> None:
    w = data_world
    w.bot.visibility = BotVisibility.PUBLIC
    runtime = runtime_for(w)
    c = await expenses(w)
    await join_bot(w.session, w.bot, 1001, "en")
    await w.session.commit()
    alice = DataService(runtime.sessionmaker, w.pricing, w.bot.id, w.alice.id)
    saved = await alice.add("expenses", {"amount": 7})
    delete = f"data:del:{c.id}:{int(saved.split('#')[1].split(':')[0])}"
    owner = TgUser(id=1001, is_bot=False, first_name="O")

    assert not alerted(await press(w, runtime, delete, ALICE))
    again = await press(w, runtime, delete, ALICE)
    assert not alerted(again)
    assert any(isinstance(c.args[0], EditMessageText) for c in again.await_args_list)
    # The owner cannot read Alice's personal record, deleted or not.
    assert alerted(await press(w, runtime, delete, owner))


async def test_forged_buttons_get_a_plain_answer(data_world) -> None:
    w = data_world
    runtime = runtime_for(w)
    await expenses(w)
    for forged in ("data:del:²:1", f"data:del:1:{2**63}", "data:del:1:0"):
        client = await press(w, runtime, forged, ALICE)
        assert not alerted(client)


async def test_inaccessible_message_is_not_edited() -> None:
    from aiogram.types import InaccessibleMessage

    message = InaccessibleMessage(chat=Chat(id=1, type="private"), message_id=3)
    query = SimpleNamespace(message=message)
    await data_chat._edit(query, "x")  # no AttributeError
    await data_chat._edit(SimpleNamespace(message=None), "x")


async def test_data_filter_keeps_integers_through_the_agent() -> None:
    from pydantic_ai import Agent
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    seen = []

    class Recorder:
        async def find(self, collection, filters, *args):
            seen.append(filters)
            return "none"

    async def respond(messages, info):
        if len(messages) == 1:
            args = {"collection": "rooms", "filters": [{"field": "room", "op": "eq", "value": 12}]}
            return ModelResponse(parts=[ToolCallPart("data_find", args)])
        return ModelResponse(parts=[TextPart("done")])

    tools = [t for t in build_tools([], data=True) if t.name == "data_find"]
    agent = Agent(FunctionModel(respond), tools=tools, deps_type=ToolServices)
    services = ToolServices(http=None, meter=None, data=Recorder())
    await agent.run("go", deps=services)

    value = seen[0][0]["value"]
    assert value == 12 and type(value) is int


def test_personal_data_is_a_toolbox_switch_not_a_tool() -> None:
    from obot.core.tools import PERSONAL_DATA
    from obot.factory.designer import ToolName

    assert ToolName.PERSONAL_DATA.value == PERSONAL_DATA == "personal_data"
    assert [t.name for t in build_tools([PERSONAL_DATA])] == []


async def test_switch_gives_data_tools_without_collections(data_world) -> None:
    w = data_world
    runtime = runtime_for(w)
    w.bot.tools = ["personal_data"]
    kit = await toolkit(w.session, runtime, w.bot, datetime.now(UTC), w.alice, interactive=True)
    assert set(DATA_TOOLS) <= {t.name for t in kit.tools}
    assert "no data collections yet" in kit.note
    in_group = await toolkit(w.session, runtime, w.bot, datetime.now(UTC), w.alice, data=False)
    assert not {t.name for t in in_group.tools} & set(DATA_TOOLS)


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, object] = {}

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.data:
            return False
        self.data[key] = value
        return True

    async def get(self, key):
        return self.data.get(key)

    async def delete(self, key):
        return int(self.data.pop(key, None) is not None)


WEIGHT = [
    {"key": "day", "label": "Day", "type": "date", "required": True},
    {"key": "kg", "label": "Weight", "type": "number", "required": True},
]


def test_define_tool_only_with_the_switch() -> None:
    assert DEFINE_TOOL not in {t.name for t in build_tools([], data=True)}
    assert DEFINE_TOOL in {t.name for t in build_tools([], data=True, define=True)}


async def test_define_tool_needs_a_data_service() -> None:
    ctx = SimpleNamespace(deps=ToolServices(http=None, meter=None))
    assert (await data_define(ctx, "weight", "Weight", [])).startswith("ERROR:")


async def test_toolkit_offers_data_define_only_with_the_switch(data_world) -> None:
    w = data_world
    runtime = runtime_for(w)
    await expenses(w)
    now = datetime.now(UTC)
    plain = await toolkit(w.session, runtime, w.bot, now, w.alice, interactive=True)
    w.bot.tools = ["personal_data"]
    switched = await toolkit(w.session, runtime, w.bot, now, w.alice, interactive=True)
    background = await toolkit(w.session, runtime, w.bot, now, w.alice)
    assert DEFINE_TOOL not in {t.name for t in plain.tools}
    assert DEFINE_TOOL in {t.name for t in switched.tools}
    assert DEFINE_TOOL not in {t.name for t in background.tools}
    assert DEFINE_HINT in switched.note
    assert DEFINE_HINT not in (background.note or "")


async def press_define(w, runtime, data: str, who: TgUser, session=None) -> AsyncMock:
    client = AsyncMock()
    message = Message(
        message_id=4, date=datetime.now(UTC), chat=Chat(id=who.id, type="private"), text="card"
    ).as_(client)
    query = CallbackQuery(id="q", from_user=who, chat_instance="c", data=data, message=message).as_(
        client
    )
    await data_chat.on_define_button(query, session or w.session, w.bot, runtime)
    return client


async def test_save_creates_the_collection_only_for_its_person(data_world) -> None:
    w = data_world
    w.bot.visibility = BotVisibility.PUBLIC
    runtime = runtime_for(w)
    runtime.redis = FakeRedis()
    await w.session.commit()
    service = DataService(
        runtime.sessionmaker,
        w.pricing,
        w.bot.id,
        w.alice.id,
        propose_define=DefineProposer(runtime, w.bot, w.alice),
    )

    told = await service.define("weight", "My weight", WEIGHT)

    assert "Save" in told
    call = runtime.sender.send_text.await_args
    assert call.args[1] == ALICE.id and "My weight" in call.args[2]
    save, cancel = (b.callback_data for b in call.kwargs["buttons"].inline_keyboard[0])
    assert save.startswith("datadef:ok:") and cancel.startswith("datadef:no:")

    stranger = await press_define(w, runtime, save, BOB)
    alert = stranger.await_args.args[0]
    assert isinstance(alert, AnswerCallbackQuery) and alert.show_alert
    assert "no collection named 'weight'" in await service.find("weight")

    await press_define(w, runtime, save, ALICE)
    assert await service.find("weight") == "No records."  # it exists now
    again = await press_define(w, runtime, save, ALICE)  # second press: expired, no crash
    assert again.await_args.args[0].text == t("fa", "data.define_expired")


async def test_cancel_drops_the_proposal(data_world) -> None:
    w = data_world
    w.bot.visibility = BotVisibility.PUBLIC
    runtime = runtime_for(w)
    runtime.redis = FakeRedis()
    await w.session.commit()
    service = DataService(
        runtime.sessionmaker,
        w.pricing,
        w.bot.id,
        w.alice.id,
        propose_define=DefineProposer(runtime, w.bot, w.alice),
    )
    await service.define("weight", "My weight", WEIGHT)
    save, cancel = (
        b.callback_data
        for b in runtime.sender.send_text.await_args.kwargs["buttons"].inline_keyboard[0]
    )

    await press_define(w, runtime, cancel, ALICE)
    await press_define(w, runtime, save, ALICE)

    assert runtime.redis.data == {}
    assert "no collection named 'weight'" in await service.find("weight")


async def test_unparseable_proposal_is_treated_as_expired(data_world) -> None:
    w = data_world
    runtime = runtime_for(w)
    runtime.redis = FakeRedis()
    runtime.redis.data["data-define:tok"] = "not json"
    await press_define(w, runtime, "datadef:ok:tok", ALICE)
    assert runtime.redis.data == {}


async def test_two_presses_apply_once(data_world) -> None:
    w = data_world
    w.bot.visibility = BotVisibility.PUBLIC
    runtime = runtime_for(w)
    runtime.redis = FakeRedis()
    await w.session.commit()
    service = DataService(
        runtime.sessionmaker,
        w.pricing,
        w.bot.id,
        w.alice.id,
        propose_define=DefineProposer(runtime, w.bot, w.alice),
    )
    await service.define("weight", "My weight", WEIGHT)
    save = runtime.sender.send_text.await_args.kwargs["buttons"].inline_keyboard[0][0].callback_data

    async with runtime.sessionmaker() as other:
        first, second = await asyncio.gather(
            press_define(w, runtime, save, ALICE),
            press_define(w, runtime, save, ALICE, session=other),
        )

    texts = [c.await_args.args[0].text for c in (first, second)]
    assert sorted(texts) == sorted(
        [t("fa", "data.define_saved", title="My weight"), t("fa", "data.define_expired")]
    )
    assert await service.find("weight") == "No records."


async def test_unexpected_failure_while_saving_is_reported(data_world, monkeypatch) -> None:
    w = data_world
    w.bot.visibility = BotVisibility.PUBLIC
    runtime = runtime_for(w)
    runtime.redis = FakeRedis()
    await w.session.commit()
    service = DataService(
        runtime.sessionmaker,
        w.pricing,
        w.bot.id,
        w.alice.id,
        propose_define=DefineProposer(runtime, w.bot, w.alice),
    )
    await service.define("weight", "My weight", WEIGHT)
    save = runtime.sender.send_text.await_args.kwargs["buttons"].inline_keyboard[0][0].callback_data

    async def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(data_chat, "apply_definition", boom)
    client = await press_define(w, runtime, save, ALICE)

    assert "try again later" in client.await_args.args[0].text


async def test_proposal_is_not_stored_when_the_send_fails(data_world) -> None:
    w = data_world
    runtime = runtime_for(w)
    runtime.redis = FakeRedis()
    runtime.sender.send_text = AsyncMock(side_effect=RuntimeError("telegram down"))
    service = DataService(
        runtime.sessionmaker,
        w.pricing,
        w.bot.id,
        w.alice.id,
        propose_define=DefineProposer(runtime, w.bot, w.alice),
    )
    await service.define("weight", "My weight", WEIGHT)
    assert runtime.redis.data == {}


SHOPPING = CollectionSchema.model_validate(
    {"fields": [{"key": "item", "label": {"en": "Item"}, "type": "text", "required": True}]}
)


async def shopping(w):
    c = await create_collection(
        w.session,
        w.pricing,
        bot=w.bot,
        key="shopping",
        title={"en": "Shopping"},
        scope=CollectionScope.GROUP,
        schema=SHOPPING,
    )
    await w.session.commit()
    return c


def roles(monkeypatch, by_tg_id: dict[int, GroupRole | None]) -> list[tuple[int, int]]:
    asked = []

    async def fake_role(client, chat_id, tg_user_id):
        asked.append((chat_id, tg_user_id))
        return by_tg_id.get(tg_user_id)

    monkeypatch.setattr(data_chat, "group_role", fake_role)
    return asked


async def remaining_items(runtime, w) -> list[int]:
    async with runtime.sessionmaker() as s:
        space = await w.space(w.alice, tg_chat_id=-100)
        c = await get_collection(s, space, "shopping")
        return [r.id for r in await find_records(s, space, c)]


async def test_group_delete_card_goes_private_and_names_the_group(data_world) -> None:
    w = data_world
    w.bot.visibility = BotVisibility.PUBLIC
    runtime = runtime_for(w)
    await shopping(w)
    group = GroupChat(-100, AsyncMock(return_value=False), title="Family")
    alice = DataService(
        runtime.sessionmaker,
        w.pricing,
        w.bot.id,
        w.alice.id,
        group=group,
        confirm_delete=DeleteConfirmer(runtime, w.bot, w.alice, group=group),
    )
    rid = int((await alice.add("shopping", {"item": "milk"})).split("#")[1].split(":")[0])

    told = await alice.delete("shopping", rid)

    assert "private chat" in told
    call = runtime.sender.send_text.await_args
    assert call.args[1] == ALICE.id  # her private chat, not the group
    assert "Family" in call.args[2] and "milk" in call.args[2]
    delete = call.kwargs["buttons"].inline_keyboard[0][0].callback_data
    assert delete.endswith(":-100") and delete.startswith("data:del:")


async def test_group_delete_button_rechecks_the_group(data_world, monkeypatch) -> None:
    w = data_world
    w.bot.visibility = BotVisibility.PUBLIC
    runtime = runtime_for(w)
    c = await shopping(w)
    alice = DataService(
        runtime.sessionmaker,
        w.pricing,
        w.bot.id,
        w.alice.id,
        group=GroupChat(-100, AsyncMock(return_value=False)),
    )
    rid = int((await alice.add("shopping", {"item": "milk"})).split("#")[1].split(":")[0])
    delete = f"data:del:{c.id}:{rid}:-100"

    # Bob is a plain member: not his item.
    asked = roles(monkeypatch, {BOB.id: GroupRole(is_admin=False), ALICE.id: None})
    assert alerted(await press(w, runtime, delete, BOB))
    # Alice left the group: she can no longer delete from it.
    assert alerted(await press(w, runtime, delete, ALICE))
    assert (-100, BOB.id) in asked and (-100, ALICE.id) in asked
    assert await remaining_items(runtime, w) == [rid]

    # Bob became an admin: Telegram is asked again, never a cache.
    roles(monkeypatch, {BOB.id: GroupRole(is_admin=True)})
    assert not alerted(await press(w, runtime, delete, BOB))
    assert await remaining_items(runtime, w) == []


async def test_group_delete_button_for_the_author(data_world, monkeypatch) -> None:
    w = data_world
    w.bot.visibility = BotVisibility.PUBLIC
    runtime = runtime_for(w)
    c = await shopping(w)
    alice = DataService(
        runtime.sessionmaker,
        w.pricing,
        w.bot.id,
        w.alice.id,
        group=GroupChat(-100, AsyncMock(return_value=False)),
    )
    rid = int((await alice.add("shopping", {"item": "milk"})).split("#")[1].split(":")[0])
    roles(monkeypatch, {ALICE.id: GroupRole(is_admin=False)})

    # The same record pressed from another group's card is not found there.
    assert alerted(await press(w, runtime, f"data:del:{c.id}:{rid}:-200", ALICE))
    assert not alerted(await press(w, runtime, f"data:del:{c.id}:{rid}:-100", ALICE))
    assert await remaining_items(runtime, w) == []


async def test_group_card_callback_fits_telegrams_limit() -> None:
    assert len(f"data:del:{MAX_ID}:{MAX_ID}:-1009999999999".encode()) <= 64


async def test_forged_group_buttons_get_a_plain_answer(data_world, monkeypatch) -> None:
    w = data_world
    runtime = runtime_for(w)
    await shopping(w)
    roles(monkeypatch, {})
    for forged in ("data:del:1:1:100", "data:del:1:1:-", "data:del:1:1:-x", "data:del:1:1:-1:2"):
        assert not alerted(await press(w, runtime, forged, ALICE))


async def test_toolkit_in_a_group_uses_only_the_groups_collections(data_world, monkeypatch) -> None:
    from obot.telegram import toolkit as toolkit_module

    w = data_world
    runtime = runtime_for(w)
    await expenses(w)
    await shopping(w)
    peer = SimpleNamespace(username="peer", display_name="Peer", persona=None)
    monkeypatch.setattr(toolkit_module, "user_bots", AsyncMock(return_value=[peer]))
    seen = {}

    class Spy:
        def __init__(self, runtime, bot, payer, **kwargs) -> None:
            seen.update(kwargs)

    monkeypatch.setattr(toolkit_module, "Delegator", Spy)
    group = GroupChat(-100, AsyncMock(return_value=False), title="Family")

    kit = await toolkit(w.session, runtime, w.bot, datetime.now(UTC), w.alice, group=group)

    assert set(DATA_TOOLS) <= {t.name for t in kit.tools}
    assert DEFINE_TOOL not in {t.name for t in kit.tools}
    assert "shopping" in kit.note and "expenses" not in kit.note
    assert seen == {"data": False}  # ask_bot from a group gets no data
    assert "milk" not in await kit.services.data.find("shopping")
    await kit.services.data.add("shopping", {"item": "milk"})
    async with runtime.sessionmaker() as s:
        space = await w.space(w.alice, tg_chat_id=-100)
        c = await get_collection(s, space, "shopping")
        assert [r.data["item"] for r in await find_records(s, space, c)] == ["milk"]


async def test_private_toolkit_names_group_lists_without_tools_for_them(data_world) -> None:
    w = data_world
    runtime = runtime_for(w)
    await shopping(w)
    kit = await toolkit(w.session, runtime, w.bot, datetime.now(UTC), w.alice)
    assert "Shopping" in kit.note and "group" in kit.note
    assert not {t.name for t in kit.tools} & set(DATA_TOOLS)
