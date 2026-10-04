from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import Chat, Message, MessageEntity, User
from redis.asyncio import Redis
from sqlalchemy import select

from obot.bots.users import has_started, join_bot, leave_bot
from obot.config import get_settings
from obot.db.models import Bot as BotRecord
from obot.db.models import User as UserRecord
from obot.telegram import bots as bots_module
from tests.test_history import make_bot

BOT_ID = 42
USERNAME = "dw_bot"
GROUP = Chat(id=-100, type="supergroup", title="G")
ALI = User(id=5005, is_bot=False, first_name="Ali", language_code="fa")
OTHER_BOT = User(id=7, is_bot=True, first_name="Other", username="other_bot")


@pytest.fixture
async def redis():
    client = Redis.from_url(get_settings().redis_url, db=15)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


async def bot_record(session, cipher) -> BotRecord:
    record = await session.get(BotRecord, await make_bot(session, cipher, 1))
    record.username = USERNAME
    return record


async def test_starting_a_bot_creates_a_free_account(session, cipher) -> None:
    bot = await bot_record(session, cipher)

    user, first = await join_bot(session, bot, ALI.id, "fa")

    assert first and user.plan == "free" and user.timezone == "Asia/Tehran"
    assert await has_started(session, bot, ALI.id)
    _, again = await join_bot(session, bot, ALI.id, "fa")
    assert not again
    assert (
        len(list(await session.scalars(select(UserRecord).where(UserRecord.tg_user_id == ALI.id))))
        == 1
    )


async def test_blocking_ends_use_until_started_again(session, cipher) -> None:
    bot = await bot_record(session, cipher)
    await join_bot(session, bot, ALI.id, None)

    await leave_bot(session, bot, ALI.id)
    assert not await has_started(session, bot, ALI.id)

    _, back = await join_bot(session, bot, ALI.id, None)
    assert back and await has_started(session, bot, ALI.id)


def group_message(text: str, sender: User = ALI, mention: bool = True) -> Message:
    entities = None
    if mention and f"@{USERNAME}" in text:
        entities = [MessageEntity(type="mention", offset=text.index("@"), length=len(USERNAME) + 1)]
    return Message(
        message_id=10,
        date=datetime.now(UTC),
        chat=GROUP,
        from_user=sender,
        text=text,
        entities=entities,
    )


class Group:
    """Feeds group messages to the bot handler; records answers and notices."""

    def __init__(self, session, record: BotRecord, redis, monkeypatch) -> None:
        self.session, self.record = session, record
        self.answered: list[str] = []
        self.runtime = SimpleNamespace(redis=redis, sender=AsyncMock())
        self.client = AsyncMock(id=BOT_ID)

        async def fake_respond(inbound, text, *args) -> None:
            self.answered.append(text)

        monkeypatch.setattr(bots_module, "_respond", fake_respond)

    async def send(self, message: Message) -> None:
        await bots_module.on_group_text(
            message.as_(self.client), self.session, self.record, self.runtime
        )

    @property
    def notices(self) -> list[str]:
        return [c.args[2] for c in self.runtime.sender.send_text.await_args_list]


async def test_group_answers_only_people_who_started_and_address_it(
    session, cipher, redis, monkeypatch
) -> None:
    group = Group(session, await bot_record(session, cipher), redis, monkeypatch)
    await join_bot(session, group.record, ALI.id, "fa")

    await group.send(group_message("just chatting"))
    await group.send(group_message(f"@{USERNAME} hi"))
    await group.send(group_message(f"@{USERNAME} from a bot", sender=OTHER_BOT))

    assert group.answered == ["hi"]
    assert group.notices == []


async def test_people_who_never_started_are_asked_once_a_day(
    session, cipher, redis, monkeypatch
) -> None:
    group = Group(session, await bot_record(session, cipher), redis, monkeypatch)

    await group.send(group_message(f"@{USERNAME} hi"))
    await group.send(group_message(f"@{USERNAME} hello?"))

    assert group.answered == []
    assert group.notices == ["برای استفاده از من، اول توی پیوی استارتم کن."]
    buttons = group.runtime.sender.send_text.await_args.kwargs["buttons"]
    assert buttons.inline_keyboard[0][0].url == f"https://t.me/{USERNAME}?start=g"
