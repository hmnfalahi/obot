from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import CallbackQuery, Chat, Message, User
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from obot.bots.apis import key_values, write_endpoint
from obot.bots.users import join_bot
from obot.config import get_settings
from obot.db.models import ApiEndpoint, BotVisibility
from obot.db.models import Bot as BotRecord
from obot.db.models import Message as StoredMessage
from obot.telegram import api_keys
from obot.telegram.api_keys import KeyRequester, WaitingForKey
from tests.test_history import make_bot

ALI = User(id=5005, is_bot=False, first_name="Ali", language_code="en")
PRIVATE = Chat(id=ALI.id, type="private")


@pytest.fixture
async def redis():
    client = Redis.from_url(get_settings().redis_url, db=15)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


@pytest.fixture
async def shop_bot(session, cipher) -> BotRecord:
    bot = await session.get(
        BotRecord, await make_bot(session, cipher, 1), options=[joinedload(BotRecord.owner)]
    )
    bot.visibility = BotVisibility.PUBLIC
    row = ApiEndpoint(bot_id=bot.id)
    write_endpoint(
        row,
        name="orders",
        description="d",
        method="GET",
        url="https://shop.example/o",
        headers={"Authorization": "Bearer {shop_token}"},
    )
    session.add(row)
    await join_bot(session, bot, ALI.id, "en")
    await session.flush()
    return bot


def runtime_with(redis, cipher) -> SimpleNamespace:
    return SimpleNamespace(
        redis=redis, cipher=cipher, sender=AsyncMock(), factory_username="obot_factory_bot"
    )


async def test_missing_keys_get_buttons_in_private_but_never_in_groups(
    session, shop_bot, redis, cipher
) -> None:
    runtime = runtime_with(redis, cipher)

    private = await KeyRequester(runtime, shop_bot, AsyncMock(), ALI.id, "en")(["shop_token"])
    group = await KeyRequester(runtime, shop_bot, AsyncMock(), -100, "en")(["shop_token"])

    buttons = runtime.sender.send_text.await_args.kwargs["buttons"].inline_keyboard
    assert buttons[0][0].callback_data == "apikey:shop_token"
    assert buttons[1][0].url == f"https://t.me/obot_factory_bot?start=mine_{shop_bot.id}"
    assert "buttons" in private
    assert runtime.sender.send_text.await_count == 1
    assert group.startswith("ERROR") and "private chat" in group


async def press(session, bot, runtime, data: str) -> None:
    client = AsyncMock()
    card = Message(message_id=2, date=datetime.now(UTC), chat=PRIVATE, text="card").as_(client)
    query = CallbackQuery(id="q", from_user=ALI, chat_instance="c", data=data, message=card)
    await api_keys.on_key_button(query.as_(client), session, bot, runtime)


def text_message(text: str) -> Message:
    return Message(
        message_id=9, date=datetime.now(UTC), chat=PRIVATE, from_user=ALI, text=text
    ).as_(AsyncMock())


async def test_a_key_sent_in_chat_is_stored_deleted_and_never_recorded(
    session, shop_bot, redis, cipher
) -> None:
    runtime = runtime_with(redis, cipher)
    await press(session, shop_bot, runtime, "apikey:shop_token")
    secret = text_message("  ali-s3cret  ")

    matched = await WaitingForKey()(secret, runtime=runtime, bot_record=shop_bot)
    await api_keys.on_key_value(secret, session, shop_bot, runtime, **matched)

    user_keys = await key_values(session, cipher, await _user(session), shop_bot)
    assert user_keys == {"shop_token": "ali-s3cret"}
    secret.bot.assert_awaited()  # deleteMessage went out
    assert await session.scalar(select(StoredMessage)) is None
    assert await WaitingForKey()(text_message("hi"), runtime=runtime, bot_record=shop_bot) is False


async def _user(session):
    from obot.bots.service import get_or_create_user

    return await get_or_create_user(session, ALI.id)


async def test_a_command_cancels_waiting_and_unknown_keys_are_ignored(
    session, shop_bot, redis, cipher
) -> None:
    runtime = runtime_with(redis, cipher)
    await press(session, shop_bot, runtime, "apikey:not_a_slot")
    assert await WaitingForKey()(text_message("x"), runtime=runtime, bot_record=shop_bot) is False

    await press(session, shop_bot, runtime, "apikey:shop_token")
    command = text_message("/tasks")
    assert await WaitingForKey()(command, runtime=runtime, bot_record=shop_bot) is False
    assert await WaitingForKey()(text_message("x"), runtime=runtime, bot_record=shop_bot) is False
