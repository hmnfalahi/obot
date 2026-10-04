"""Deciding whether a bot answers a group message nobody addressed to it."""

import logging
import time
from collections import OrderedDict
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from obot.billing.usage import check_llm_quota, record_llm_usage
from obot.core.memory import Embedder
from obot.core.rules import (
    TOPIC_SIMILARITY_THRESHOLD,
    ReplyRule,
    RuleKind,
    classify,
    cosine,
    matches_locally,
    parse_rules,
)
from obot.db.models import Bot, UsageKind
from obot.telegram.runtime import Runtime

log = logging.getLogger(__name__)

# Unprompted replies per chat per hour, and the pause after a rule fires.
MAX_RULE_REPLIES_PER_HOUR = 20
RULE_COOLDOWN_SECONDS = 60
# Very short messages ("ok", "👍") are never worth a topic check.
MIN_TOPIC_TEXT_LENGTH = 8
TOPIC_CACHE_SIZE = 512


class TopicEmbeddings:
    """Embeddings of rule descriptions, which rarely change, kept in memory."""

    def __init__(self, size: int = TOPIC_CACHE_SIZE) -> None:
        self._size = size
        self._cache: OrderedDict[str, list[float]] = OrderedDict()

    async def get(self, embedder: Embedder, topics: list[str]) -> tuple[list[list[float]], int]:
        """Vectors for `topics` and the tokens spent embedding the uncached ones."""
        missing = [t for t in dict.fromkeys(topics) if t not in self._cache]
        tokens = 0
        if missing:
            result = await embedder.embed(missing)
            tokens = result.tokens
            for topic, vector in zip(missing, result.vectors, strict=True):
                self._cache[topic] = vector
        for topic in topics:
            self._cache.move_to_end(topic)
        while len(self._cache) > self._size:
            self._cache.popitem(last=False)
        return [self._cache[t] for t in topics], tokens


topic_embeddings = TopicEmbeddings()


async def _allowed_to_fire(runtime: Runtime, bot: Bot, chat_id: int, rule_index: int) -> bool:
    hour = int(time.time() // 3600)
    count_key = f"rule-replies:{bot.id}:{chat_id}:{hour}"
    async with runtime.redis.pipeline(transaction=True) as pipe:
        pipe.incr(count_key)
        pipe.expire(count_key, 3600)
        count, _ = await pipe.execute()
    if count > MAX_RULE_REPLIES_PER_HOUR:
        return False
    cooldown_key = f"rule-cooldown:{bot.id}:{chat_id}:{rule_index}"
    return bool(await runtime.redis.set(cooldown_key, 1, nx=True, ex=RULE_COOLDOWN_SECONDS))


async def pick_rule(
    session: AsyncSession,
    runtime: Runtime,
    bot: Bot,
    chat_id: int,
    text: str,
    now: datetime,
) -> ReplyRule | None:
    """The first rule that wants a reply to `text`, honouring quota and throttling."""
    rules = parse_rules(bot.reply_rules or [])
    if not rules:
        return None
    # Unprompted replies stay silent when out of quota, instead of announcing it.
    if not (await check_llm_quota(session, runtime.pricing, bot.owner, bot, now)).allowed:
        return None

    chosen: tuple[int, ReplyRule] | None = None
    for index, rule in enumerate(rules):
        if matches_locally(rule, text):
            chosen = (index, rule)
            break

    if chosen is None and len(text) >= MIN_TOPIC_TEXT_LENGTH:
        topics = [(i, r) for i, r in enumerate(rules) if r.kind == RuleKind.TOPIC]
        if topics:
            chosen = await _match_topic(session, runtime, bot, text, topics)

    if chosen is None or not await _allowed_to_fire(runtime, bot, chat_id, chosen[0]):
        return None
    return chosen[1]


async def _match_topic(
    session: AsyncSession,
    runtime: Runtime,
    bot: Bot,
    text: str,
    topics: list[tuple[int, ReplyRule]],
) -> tuple[int, ReplyRule] | None:
    message = await runtime.embedder.embed([text])
    vectors, topic_tokens = await topic_embeddings.get(
        runtime.embedder, [r.topic for _, r in topics]
    )
    await record_llm_usage(
        session,
        runtime.pricing,
        bot.owner,
        bot,
        message.model_name,
        message.tokens + topic_tokens,
        0,
        kind=UsageKind.MEMORY,
    )
    scored = sorted(
        ((cosine(message.vectors[0], v), i, r) for (i, r), v in zip(topics, vectors, strict=True)),
        key=lambda item: item[0],
        reverse=True,
    )
    for score, index, rule in scored:
        if score < TOPIC_SIMILARITY_THRESHOLD:
            break
        verdict = await classify(runtime.models.for_tier("cheap"), rule.topic, text)
        await record_llm_usage(
            session,
            runtime.pricing,
            bot.owner,
            bot,
            verdict.model_name,
            verdict.input_tokens,
            verdict.output_tokens,
            kind=UsageKind.MEMORY,
        )
        log.info(
            "topic rule of bot %s: similarity %.2f, relevant=%s", bot.id, score, verdict.relevant
        )
        if verdict.relevant:
            return index, rule
    return None


def welcome_rule(bot: Bot) -> ReplyRule | None:
    return next(
        (r for r in parse_rules(bot.reply_rules or []) if r.kind == RuleKind.NEW_MEMBER), None
    )
