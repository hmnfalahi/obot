from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from pydantic_ai.models.test import TestModel
from redis.asyncio import Redis

from obot.billing.plans import ModelPrice, Pricing
from obot.bots.service import ManagedBotInfo, register_managed_bot
from obot.config import get_settings
from obot.core.memory import Embeddings
from obot.core.rules import (
    ReplyRule,
    RuleKind,
    cosine,
    matches_locally,
    parse_rules,
    rule_note,
)
from obot.telegram import reply_rules
from obot.telegram.reply_rules import TopicEmbeddings, pick_rule, welcome_rule
from tests.test_api import ALICE, auth, make_bot

PLANS_FILE = Path(__file__).parent.parent / "plans.yaml"


def test_keyword_matching_is_case_insensitive() -> None:
    rule = ReplyRule(kind=RuleKind.KEYWORD, keywords=["Price", "قیمت"])

    assert matches_locally(rule, "what's the PRICE today?")
    assert matches_locally(rule, "قیمت دلار چنده")
    assert not matches_locally(rule, "hello")


def test_regex_rule_times_out_instead_of_hanging() -> None:
    slow = ReplyRule(kind=RuleKind.REGEX, pattern=r"^(a|a)*$")

    assert not matches_locally(slow, "a" * 40 + "b")
    assert matches_locally(ReplyRule(kind=RuleKind.REGEX, pattern=r"\d{3}"), "code 123")


def test_invalid_pattern_is_rejected() -> None:
    with pytest.raises(ValidationError, match="invalid pattern"):
        ReplyRule(kind=RuleKind.REGEX, pattern="(")


def test_parse_rules_skips_broken_and_incomplete() -> None:
    raw = [
        {"kind": "keyword", "keywords": ["a"]},
        {"kind": "keyword", "keywords": [" "]},
        {"kind": "regex", "pattern": "("},
        {"kind": "topic"},
        {"kind": "nonsense"},
        {"kind": "new_member"},
    ]

    assert [r.kind for r in parse_rules(raw)] == [RuleKind.KEYWORD, RuleKind.NEW_MEMBER]


def test_rule_note_explains_the_reason() -> None:
    note = rule_note(ReplyRule(kind=RuleKind.TOPIC, topic="our shop", instruction="Be brief."))

    assert "our shop" in note and "Be brief." in note and "Nobody mentioned you" in note


def test_cosine() -> None:
    assert cosine([1, 0], [1, 0]) == pytest.approx(1)
    assert cosine([1, 0], [0, 1]) == pytest.approx(0)
    assert cosine([0, 0], [1, 1]) == 0


class FakeEmbedder:
    model = "baai/bge-m3"

    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self.vectors = vectors
        self.calls: list[list[str]] = []

    async def embed(self, texts):
        self.calls.append(list(texts))
        return Embeddings([self.vectors.get(t, [0.0, 1.0]) for t in texts], self.model, 5)


async def test_topic_embeddings_are_cached() -> None:
    cache = TopicEmbeddings(size=2)
    embedder = FakeEmbedder({"a": [1.0, 0.0]})

    first, tokens = await cache.get(embedder, ["a", "b"])
    again, again_tokens = await cache.get(embedder, ["a"])

    assert first[0] == [1.0, 0.0] and again[0] == [1.0, 0.0]
    assert tokens == 5 and again_tokens == 0
    assert embedder.calls == [["a", "b"]]


@pytest.fixture
async def redis():
    client = Redis.from_url(get_settings().redis_url, db=15)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


async def rule_bot(session, cipher, rules: list[dict]):
    async def fetch_token(_: int) -> str:
        return "5:t"

    result = await register_managed_bot(
        session, cipher, fetch_token, 1001, ManagedBotInfo(5, "r_bot", "R"), 10
    )
    result.bot.reply_rules = rules
    await session.flush()
    return result.bot


def runtime_for(redis, embedder, verdict: bool):
    pricing = Pricing.load(PLANS_FILE)
    pricing.models["test"] = ModelPrice(input_per_mtok=1, output_per_mtok=1)
    model = TestModel(custom_output_args={"relevant": verdict})
    return SimpleNamespace(
        redis=redis,
        embedder=embedder,
        pricing=pricing,
        models=SimpleNamespace(for_tier=lambda tier: model),
    )


async def test_keyword_rule_fires_then_cools_down(session, cipher, redis) -> None:
    bot = await rule_bot(session, cipher, [{"kind": "keyword", "keywords": ["price"]}])
    runtime = runtime_for(redis, FakeEmbedder({}), verdict=False)
    now = datetime.now(UTC)

    first = await pick_rule(session, runtime, bot, -100, "the price?", now)
    second = await pick_rule(session, runtime, bot, -100, "price again?", now)
    other_chat = await pick_rule(session, runtime, bot, -200, "price?", now)

    assert first.kind == RuleKind.KEYWORD
    assert second is None  # cooldown
    assert other_chat is not None


async def test_topic_rule_needs_similarity_and_classifier(session, cipher, redis) -> None:
    rules = [{"kind": "topic", "topic": "shop orders"}]
    vectors = {"shop orders": [1.0, 0.0], "where is my order?": [0.9, 0.1]}

    bot = await rule_bot(session, cipher, rules)
    yes = runtime_for(redis, FakeEmbedder(vectors), verdict=True)
    assert (await pick_rule(session, yes, bot, -1, "where is my order?", datetime.now(UTC))).topic

    no = runtime_for(redis, FakeEmbedder(vectors), verdict=False)
    assert await pick_rule(session, no, bot, -2, "where is my order?", datetime.now(UTC)) is None

    # Dissimilar messages never reach the classifier.
    unrelated = runtime_for(redis, FakeEmbedder({"shop orders": [1.0, 0.0]}), verdict=True)
    assert (
        await pick_rule(session, unrelated, bot, -3, "nice weather today", datetime.now(UTC))
        is None
    )


async def test_rules_respect_hourly_cap(session, cipher, redis, monkeypatch) -> None:
    monkeypatch.setattr(reply_rules, "MAX_RULE_REPLIES_PER_HOUR", 1)
    monkeypatch.setattr(reply_rules, "RULE_COOLDOWN_SECONDS", 1)
    bot = await rule_bot(
        session,
        cipher,
        [{"kind": "keyword", "keywords": ["a"]}, {"kind": "keyword", "keywords": ["b"]}],
    )
    runtime = runtime_for(redis, FakeEmbedder({}), verdict=False)

    assert await pick_rule(session, runtime, bot, -1, "a", datetime.now(UTC))
    assert await pick_rule(session, runtime, bot, -1, "b", datetime.now(UTC)) is None


async def test_welcome_rule(session, cipher) -> None:
    bot = await rule_bot(session, cipher, [{"kind": "new_member", "instruction": "Say hi"}])

    assert welcome_rule(bot).instruction == "Say hi"


async def test_api_saves_only_complete_rules(api) -> None:
    bot_id = await make_bot(api, ALICE, 11)

    response = await api.patch(
        f"/api/bots/{bot_id}",
        json={
            "reply_rules": [
                {"kind": "topic", "topic": "our shop", "instruction": "brief"},
                {"kind": "keyword", "keywords": [""]},
            ]
        },
        headers=auth(ALICE),
    )

    assert response.status_code == 200
    assert response.json()["reply_rules"] == [
        {
            "kind": "topic",
            "keywords": [],
            "pattern": None,
            "topic": "our shop",
            "instruction": "brief",
        }
    ]
    bad = await api.patch(
        f"/api/bots/{bot_id}",
        json={"reply_rules": [{"kind": "regex", "pattern": "("}]},
        headers=auth(ALICE),
    )
    assert bad.status_code == 422
