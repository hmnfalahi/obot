from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiogram.methods import AnswerCallbackQuery
from aiogram.types import CallbackQuery, Chat, Message
from aiogram.types import User as TgUser
from sqlalchemy.ext.asyncio import async_sessionmaker

from obot.core.tools import DATA_TOOLS, ToolServices, build_tools, data_add
from obot.data.query import find_records
from obot.data.repository import create_collection, get_collection
from obot.data.schema import CollectionSchema
from obot.data.service import DataService
from obot.db.models import BotVisibility, CollectionScope
from obot.telegram import data_chat
from obot.telegram.data_chat import DeleteConfirmer, SharedNotice
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


async def test_toolkit_adds_data_tools_and_catalog_in_private_only(data_world) -> None:
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
