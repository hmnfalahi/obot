from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from aiogram.types import Chat, Message, User
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker

from obot.bots.service import ManagedBotInfo, register_managed_bot
from obot.chat.membership import bots_in_chat, upsert_membership
from obot.config import get_settings
from obot.db.models import BotStatus
from obot.telegram import bots as bots_module
from obot.telegram.conversations import (
    MAX_BOT_TURNS,
    Thread,
    addresses,
    is_new_in_thread,
    is_skip,
)

GROUP = -100


def test_thread_turn_limit() -> None:
    thread = Thread(root=1)
    for _ in range(MAX_BOT_TURNS - 1):
        thread = thread.next()
    assert thread.can_continue
    assert not thread.next().can_continue


def test_addresses_by_mention_or_reply() -> None:
    assert addresses("@Helper_bot what do you think?", None, "helper_bot", 77)
    assert addresses("sounds good", 77, "helper_bot", 77)
    assert not addresses("@helper_botx hi", None, "helper_bot", 77)
    assert not addresses("helper_bot without at", None, "helper_bot", 77)


@pytest.mark.parametrize("text", ["SKIP", " skip ", "[SKIP]", "Skip."])
def test_skip_variants(text) -> None:
    assert is_skip(text)


def test_real_answer_is_not_skip() -> None:
    assert not is_skip("Skip the intro and buy oil")


@pytest.fixture
async def redis():
    client = Redis.from_url(get_settings().redis_url, db=15)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


async def test_repeated_text_ends_the_thread(redis) -> None:
    thread = Thread(root=5)

    assert await is_new_in_thread(redis, GROUP, thread, "Oil is $90")
    assert await is_new_in_thread(redis, GROUP, thread, "Gold is up")
    assert not await is_new_in_thread(redis, GROUP, thread, "  oil IS $90 ")
    assert await is_new_in_thread(redis, GROUP, Thread(root=6), "Oil is $90")


async def make_bot(session, cipher, tg_bot_id: int, username: str):
    async def fetch_token(_: int) -> str:
        return f"{tg_bot_id}:t"

    result = await register_managed_bot(
        session, cipher, fetch_token, 1001, ManagedBotInfo(tg_bot_id, username, username), 10
    )
    await upsert_membership(
        session,
        result.bot.id,
        GROUP,
        chat_type="supergroup",
        title="G",
        is_member=True,
        is_admin=False,
    )
    return result.bot


async def test_bots_in_chat_lists_active_members_only(session, cipher) -> None:
    a = await make_bot(session, cipher, 1, "a_bot")
    b = await make_bot(session, cipher, 2, "b_bot")
    c = await make_bot(session, cipher, 3, "c_bot")
    c.status = BotStatus.DISABLED
    await session.flush()

    assert [x.id for x in await bots_in_chat(session, GROUP, exclude=a.id)] == [b.id]


def sent_message(text: str, reply_to_user: int | None = None) -> Message:
    group = Chat(id=GROUP, type="supergroup")
    reply = None
    if reply_to_user is not None:
        reply = Message(
            message_id=1,
            date=datetime.now(UTC),
            chat=group,
            from_user=User(id=reply_to_user, is_bot=True, first_name="X"),
            text="earlier",
        )
    return Message(
        message_id=50,
        date=datetime.now(UTC),
        chat=group,
        from_user=User(id=1, is_bot=True, first_name="A"),
        text=text,
        reply_to_message=reply,
    )


class Spawner:
    def __init__(self) -> None:
        self.jobs: list[tuple[int, object]] = []

    def spawn(self, tg_bot_id, key, job) -> None:
        self.jobs.append((tg_bot_id, key))


async def routing_runtime(session, redis):
    await session.commit()
    spawner = Spawner()
    runtime = SimpleNamespace(
        redis=redis,
        sessionmaker=async_sessionmaker(session.bind, expire_on_commit=False),
        transport=SimpleNamespace(client=lambda tg_bot_id: object()),
        updates=spawner,
    )
    return runtime, spawner


async def route(
    runtime, sender, text: str, thread: Thread, reply_to_user: int | None = None, **kwargs
):
    message = sent_message(text, reply_to_user)
    await bots_module.route_to_bots(runtime, sender, GROUP, message, text, thread, **kwargs)


async def test_route_delivers_only_to_addressed_bots(session, cipher, redis) -> None:
    a = await make_bot(session, cipher, 1, "a_bot")
    await make_bot(session, cipher, 2, "b_bot")
    await make_bot(session, cipher, 3, "c_bot")
    runtime, spawner = await routing_runtime(session, redis)

    await route(runtime, a, "@b_bot your turn", Thread(root=9))

    assert spawner.jobs == [(2, ("chat", GROUP))]


async def test_route_follows_replies(session, cipher, redis) -> None:
    a = await make_bot(session, cipher, 1, "a_bot")
    await make_bot(session, cipher, 3, "c_bot")
    runtime, spawner = await routing_runtime(session, redis)

    await route(runtime, a, "I agree", Thread(root=9), reply_to_user=3)

    assert [tg for tg, _ in spawner.jobs] == [3]


async def test_route_stops_at_turn_limit_and_on_repeats(session, cipher, redis) -> None:
    a = await make_bot(session, cipher, 1, "a_bot")
    await make_bot(session, cipher, 2, "b_bot")
    runtime, spawner = await routing_runtime(session, redis)

    await route(runtime, a, "@b_bot x", Thread(root=9, depth=MAX_BOT_TURNS - 1))
    assert spawner.jobs == []

    thread = Thread(root=10)
    await route(runtime, a, "@b_bot y", thread)
    await route(runtime, a, "@b_bot y", thread)
    assert len(spawner.jobs) == 1


async def test_route_never_targets_the_sender(session, cipher, redis) -> None:
    a = await make_bot(session, cipher, 1, "a_bot")
    runtime, spawner = await routing_runtime(session, redis)

    await route(runtime, a, "@a_bot talking to myself", Thread(root=9))

    assert spawner.jobs == []


def test_peers_note_lists_bots() -> None:
    from obot.telegram.conversations import peers_note

    note = peers_note([("b_bot", "Bee")])

    assert "@b_bot (Bee)" in note
    assert peers_note([]) is None


async def test_route_uses_known_author_without_telegram_echo(session, cipher, redis) -> None:
    """Telegram may omit reply_to_message; the replied-to author is passed explicitly."""
    a = await make_bot(session, cipher, 1, "a_bot")
    await make_bot(session, cipher, 3, "c_bot")
    runtime, spawner = await routing_runtime(session, redis)

    await route(runtime, a, "Thanks!", Thread(root=9), replied_to=3)

    assert [tg for tg, _ in spawner.jobs] == [3]
