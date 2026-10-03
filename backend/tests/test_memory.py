from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic_ai.models.test import TestModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from obot.billing.plans import ModelPrice, Pricing
from obot.bots.service import ManagedBotInfo, register_managed_bot
from obot.chat.history import ChatScope, recent_messages, record_message
from obot.core.memory import Embeddings, KnownFact, extract_facts
from obot.db.models import EMBEDDING_DIMENSIONS, Message, SenderKind, UsageEvent, UsageKind
from obot.memory.jobs import FULL_RECALL_LIMIT, extract_scope, recall_facts
from obot.memory.store import (
    add_facts,
    forget_chat,
    get_cursor,
    list_facts,
    purge_old_messages,
    remove_facts,
    scopes_with_new_messages,
    set_cursor,
)

PLANS_FILE = Path(__file__).parent.parent / "plans.yaml"


def vec(i: int) -> list[float]:
    """A unit vector pointing along axis i."""
    v = [0.0] * EMBEDDING_DIMENSIONS
    v[i] = 1.0
    return v


class FakeEmbedder:
    model = "baai/bge-m3"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed(self, texts):
        self.calls.append(list(texts))
        return Embeddings([vec(len(t) % 50) for t in texts], self.model, tokens=10)


async def make_bot(session, cipher, tg_bot_id: int, tools=("memory",)):
    async def fetch_token(_: int) -> str:
        return "t"

    result = await register_managed_bot(
        session, cipher, fetch_token, 1001, ManagedBotInfo(tg_bot_id, f"b{tg_bot_id}", "B"), 10
    )
    result.bot.tools = list(tools)
    await session.flush()
    return result.bot


async def test_facts_are_isolated_per_bot_and_chat(session, cipher) -> None:
    a = await make_bot(session, cipher, 1)
    b = await make_bot(session, cipher, 2)
    scopes = [ChatScope(a.id, 10), ChatScope(a.id, 20), ChatScope(b.id, 10)]
    for i, scope in enumerate(scopes):
        await add_facts(session, scope, [f"fact {i}"], [vec(i)])

    for i, scope in enumerate(scopes):
        assert [f.fact for f in await list_facts(session, scope)] == [f"fact {i}"]


async def test_remove_cannot_touch_other_chats(session, cipher) -> None:
    bot = await make_bot(session, cipher, 1)
    mine, other = ChatScope(bot.id, 10), ChatScope(bot.id, 20)
    await add_facts(session, other, ["secret"], [vec(0)])
    [secret] = await list_facts(session, other)

    await remove_facts(session, mine, [secret.id])

    assert [f.fact for f in await list_facts(session, other)] == ["secret"]


async def test_forget_clears_only_that_chat(session, cipher) -> None:
    bot = await make_bot(session, cipher, 1)
    mine, other = ChatScope(bot.id, 10), ChatScope(bot.id, 20)
    for scope in (mine, other):
        await add_facts(session, scope, ["f"], [vec(0)])
        m = await record_message(session, scope, sender_kind=SenderKind.HUMAN, text="hi")
        await set_cursor(session, scope, m.id)

    await forget_chat(session, mine)

    assert await list_facts(session, mine) == []
    assert await recent_messages(session, mine, 10) == []
    assert await get_cursor(session, mine) == 0
    assert len(await list_facts(session, other)) == 1
    assert len(await recent_messages(session, other, 10)) == 1


async def test_only_memory_bots_with_new_messages_are_pending(session, cipher) -> None:
    with_memory = await make_bot(session, cipher, 1)
    without = await make_bot(session, cipher, 2, tools=())
    for bot in (with_memory, without):
        await record_message(session, ChatScope(bot.id, 10), sender_kind=SenderKind.HUMAN, text="x")
    done = ChatScope(with_memory.id, 20)
    m = await record_message(session, done, sender_kind=SenderKind.HUMAN, text="y")
    await set_cursor(session, done, m.id)

    assert await scopes_with_new_messages(session) == [ChatScope(with_memory.id, 10)]


async def test_extract_facts_drops_unknown_ids() -> None:
    model = TestModel(custom_output_args={"add": [" Ali likes tea ", ""], "remove": [1, 999]})

    result = await extract_facts(
        model, [KnownFact(1, "Ali likes coffee")], ["Ali: I switched to tea"]
    )

    assert result.update.add == ["Ali likes tea"]
    assert result.update.remove == [1]


async def test_extract_scope_end_to_end(session, cipher) -> None:
    bot = await make_bot(session, cipher, 1)
    scope = ChatScope(bot.id, 10)
    await add_facts(session, scope, ["Ali likes coffee"], [vec(0)])
    [old] = await list_facts(session, scope)
    for text in ["I switched to tea", "see you"]:
        last = await record_message(
            session, scope, sender_kind=SenderKind.HUMAN, sender_name="Ali", text=text
        )
    await session.commit()
    last_id, old_id = last.id, old.id
    model = TestModel(custom_output_args={"add": ["Ali likes tea"], "remove": [old_id]})
    embedder = FakeEmbedder()
    sessionmaker = async_sessionmaker(session.bind, expire_on_commit=False)

    pricing = Pricing.load(PLANS_FILE)
    pricing.models["test"] = ModelPrice(input_per_mtok=1, output_per_mtok=1)

    await extract_scope(sessionmaker, pricing, model, embedder, scope)

    session.expire_all()
    assert [f.fact for f in await list_facts(session, scope)] == ["Ali likes tea"]
    assert await get_cursor(session, scope) == last_id
    assert embedder.calls == [["Ali likes tea"]]
    kinds = list(await session.scalars(select(UsageEvent.kind)))
    assert kinds == [UsageKind.MEMORY, UsageKind.MEMORY]
    assert await scopes_with_new_messages(session) == []


async def test_recall_uses_all_facts_when_few(session, cipher) -> None:
    bot = await make_bot(session, cipher, 1)
    scope = ChatScope(bot.id, 10)
    await add_facts(session, scope, ["a", "b"], [vec(0), vec(1)])
    embedder = FakeEmbedder()

    facts = await recall_facts(session, Pricing.load(PLANS_FILE), bot, scope, "q", embedder)

    assert facts == ["a", "b"]
    assert embedder.calls == []


async def test_recall_searches_nearest_when_many(session, cipher) -> None:
    bot = await make_bot(session, cipher, 1)
    scope = ChatScope(bot.id, 10)
    n = FULL_RECALL_LIMIT + 5
    await add_facts(session, scope, [f"fact {i}" for i in range(n)], [vec(i) for i in range(n)])
    embedder = FakeEmbedder()

    # FakeEmbedder maps "q" (length 1) to axis 1, closest to "fact 1".
    facts = await recall_facts(session, Pricing.load(PLANS_FILE), bot, scope, "q", embedder)

    assert facts[0] == "fact 1"
    assert len(facts) == 5


async def test_purge_removes_only_old_messages(session, cipher) -> None:
    bot = await make_bot(session, cipher, 1)
    scope = ChatScope(bot.id, 10)
    old = await record_message(session, scope, sender_kind=SenderKind.HUMAN, text="old")
    await record_message(session, scope, sender_kind=SenderKind.HUMAN, text="new")
    await session.execute(
        update(Message)
        .where(Message.id == old.id)
        .values(created_at=datetime.now(UTC) - timedelta(days=31))
    )

    assert await purge_old_messages(session, retention_days=30) == 1
    assert [m.text for m in await recent_messages(session, scope, 10)] == ["new"]
