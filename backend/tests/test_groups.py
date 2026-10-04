import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, MessageEntity, User
from redis.asyncio import Redis

from obot.chat.membership import list_memberships, upsert_membership
from obot.config import get_settings
from obot.db.models import Message as StoredMessage
from obot.db.models import SenderKind
from obot.telegram import sender as sender_module
from obot.telegram.addressing import is_addressed, strip_mention
from obot.telegram.bots import to_turn
from obot.telegram.sender import Sender

BOT_ID = 42
USERNAME = "dw_bot"
GROUP = Chat(id=-100, type="supergroup", title="G")
ALI = User(id=1, is_bot=False, first_name="Ali")
ME = User(id=BOT_ID, is_bot=True, first_name="DW", username=USERNAME)


def msg(text: str, entities=None, reply_to: Message | None = None) -> Message:
    return Message(
        message_id=10,
        date=datetime.now(UTC),
        chat=GROUP,
        from_user=ALI,
        text=text,
        entities=entities,
        reply_to_message=reply_to,
    )


def entity(kind: str, text: str, part: str, user: User | None = None) -> MessageEntity:
    return MessageEntity(type=kind, offset=text.index(part), length=len(part), user=user)


def test_mention_addresses_bot_case_insensitively() -> None:
    text = "hey @DW_bot what's up"
    assert is_addressed(msg(text, [entity("mention", text, "@DW_bot")]), BOT_ID, USERNAME)


def test_mention_of_other_bot_does_not_address() -> None:
    text = "hey @other_bot"
    assert not is_addressed(msg(text, [entity("mention", text, "@other_bot")]), BOT_ID, USERNAME)


def test_plain_message_does_not_address() -> None:
    assert not is_addressed(msg("hello everyone"), BOT_ID, USERNAME)


def test_reply_to_bot_addresses() -> None:
    previous = Message(message_id=9, date=datetime.now(UTC), chat=GROUP, from_user=ME, text="hi")
    assert is_addressed(msg("thanks", reply_to=previous), BOT_ID, USERNAME)


def test_command_aimed_at_bot_addresses() -> None:
    text = "/price@dw_bot"
    assert is_addressed(msg(text, [entity("bot_command", text, text)]), BOT_ID, USERNAME)
    other = "/price@other_bot"
    assert not is_addressed(msg(other, [entity("bot_command", other, other)]), BOT_ID, USERNAME)


def test_text_mention_addresses() -> None:
    text = "hey DW"
    assert is_addressed(msg(text, [entity("text_mention", text, "DW", ME)]), BOT_ID, USERNAME)


def test_strip_mention() -> None:
    assert strip_mention("@dw_bot  price of dollar?", USERNAME) == "price of dollar?"
    assert strip_mention("price @DW_BOT now", USERNAME) == "price now"


def test_group_turns_carry_sender_names() -> None:
    human = StoredMessage(sender_kind=SenderKind.HUMAN, sender_name="Ali", text="hi")
    mine = StoredMessage(sender_kind=SenderKind.SELF, sender_name="DW", text="hello")

    assert to_turn(human, group=True).text == "Ali: hi"
    assert to_turn(human, group=False).text == "hi"
    assert to_turn(mine, group=True).role == "assistant"
    assert to_turn(mine, group=True).text == "hello"


async def test_membership_tracks_join_promotion_and_leave(session, cipher) -> None:
    from tests.test_history import make_bot

    bot_id = await make_bot(session, cipher, 1)
    kwargs = {"chat_type": "supergroup", "title": "G"}

    await upsert_membership(session, bot_id, -100, is_member=True, is_admin=False, **kwargs)
    row = await upsert_membership(session, bot_id, -100, is_member=True, is_admin=True, **kwargs)
    assert row.is_admin and row.left_at is None

    row = await upsert_membership(session, bot_id, -100, is_member=False, is_admin=True, **kwargs)
    assert not row.is_member and not row.is_admin and row.left_at is not None
    assert await list_memberships(session, bot_id) == []


@pytest.fixture
async def redis():
    client = Redis.from_url(get_settings().redis_url, db=15)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


async def test_rate_limit_waits_for_next_window(redis, monkeypatch) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        # Jump to the next window so the retry succeeds.
        monkeypatch.setattr(time, "time", lambda: (int(now // 60) + 1) * 60 + 1)

    now = (int(time.time() // 60)) * 60 + 30
    monkeypatch.setattr(time, "time", lambda: now)
    monkeypatch.setattr(sender_module.asyncio, "sleep", fake_sleep)
    sender = Sender(redis, group_per_minute=2)

    for _ in range(2):
        await sender.acquire(BOT_ID, -100, group=True)
    assert slept == []

    await sender.acquire(BOT_ID, -100, group=True)
    assert slept == [pytest.approx(30)]


async def test_flood_control_retries_after_delay(redis, monkeypatch) -> None:
    bot = AsyncMock()
    bot.id = BOT_ID
    flood = TelegramRetryAfter(
        method=SendMessage(chat_id=-100, text="x"), message="", retry_after=3
    )
    bot.send_message.side_effect = [flood, "sent"]
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(sender_module.asyncio, "sleep", fake_sleep)

    result = await Sender(redis).send_text(bot, -100, "hello", group=True, reply_to=10)

    assert result == ["sent"]
    assert slept == [3]
    assert bot.send_message.await_args.kwargs["reply_parameters"].message_id == 10
