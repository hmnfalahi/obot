"""Background memory extraction, recall at reply time, and history retention."""

import logging
from datetime import UTC, datetime

from pydantic_ai.models import Model
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import joinedload

from obot.billing.plans import Pricing
from obot.billing.usage import QuotaReason, check_llm_quota, record_llm_usage
from obot.chat.history import ChatScope
from obot.core.memory import Embedder, KnownFact, extract_facts
from obot.db.models import Bot, SenderKind, UsageKind, User
from obot.memory.store import (
    add_facts,
    count_facts,
    get_cursor,
    list_facts,
    messages_after,
    purge_old_messages,
    remove_facts,
    scopes_with_new_messages,
    search_facts,
    set_cursor,
)

log = logging.getLogger(__name__)

EXTRACT_BATCH = 50
MAX_KNOWN_FACTS = 100
# Up to this many facts are given to the model in full; beyond it, vector search.
FULL_RECALL_LIMIT = 20
SEARCH_RECALL_LIMIT = 5
# Extraction is background work: it stops when the payer cannot pay, not when the
# daily message count is used up.
BLOCKING_REASONS = {QuotaReason.NO_CREDITS}


async def extract_pending(
    sessionmaker: async_sessionmaker[AsyncSession],
    pricing: Pricing,
    model: Model,
    embedder: Embedder,
) -> None:
    async with sessionmaker() as session:
        scopes = await scopes_with_new_messages(session)
    for scope in scopes:
        try:
            await extract_scope(sessionmaker, pricing, model, embedder, scope)
        except Exception:
            log.exception("memory extraction failed for %s", scope)


async def extract_scope(
    sessionmaker: async_sessionmaker[AsyncSession],
    pricing: Pricing,
    model: Model,
    embedder: Embedder,
    scope: ChatScope,
) -> None:
    async with sessionmaker() as session:
        bot = await session.get(Bot, scope.bot_id, options=[joinedload(Bot.owner)])
        payer = await memory_payer(session, scope)
        if payer is not None:
            quota = await check_llm_quota(session, pricing, payer, datetime.now(UTC))
            if quota.reason in BLOCKING_REASONS:
                return

        messages = await messages_after(
            session, scope, await get_cursor(session, scope), EXTRACT_BATCH
        )
        if not messages:
            return
        lines = [
            f"{'You (the bot)' if m.sender_kind == SenderKind.SELF else m.sender_name}: {m.text}"
            for m in messages
        ]
        known = [KnownFact(f.id, f.fact) for f in await list_facts(session, scope, MAX_KNOWN_FACTS)]

        result = await extract_facts(model, known, lines)
        await record_llm_usage(
            session,
            pricing,
            payer,
            bot,
            result.model_name,
            result.input_tokens,
            result.output_tokens,
            kind=UsageKind.MEMORY,
        )
        update = result.update
        if update.add:
            embeddings = await embedder.embed(update.add)
            await record_llm_usage(
                session,
                pricing,
                payer,
                bot,
                embeddings.model_name,
                embeddings.tokens,
                0,
                kind=UsageKind.MEMORY,
            )
            await add_facts(session, scope, update.add, embeddings.vectors)
        await remove_facts(session, scope, update.remove)
        await set_cursor(session, scope, messages[-1].id)
        await session.commit()
    log.info(
        "memory for bot %s chat %s: +%d -%d facts from %d messages",
        scope.bot_id,
        scope.tg_chat_id,
        len(update.add),
        len(update.remove),
        len(messages),
    )


async def memory_payer(session: AsyncSession, scope: ChatScope) -> User | None:
    """Who pays for remembering a chat: the person in a private chat, Obot in a group."""
    if scope.tg_chat_id < 0:
        return None
    return await session.scalar(select(User).where(User.tg_user_id == scope.tg_chat_id))


async def recall_facts(
    session: AsyncSession,
    pricing: Pricing,
    bot: Bot,
    payer: User,
    scope: ChatScope,
    query: str,
    embedder: Embedder,
) -> list[str]:
    """Facts to show the model for a reply: all of them, or the closest few."""
    if await count_facts(session, scope) <= FULL_RECALL_LIMIT:
        return [f.fact for f in await list_facts(session, scope)]
    embeddings = await embedder.embed([query])
    await record_llm_usage(
        session,
        pricing,
        payer,
        bot,
        embeddings.model_name,
        embeddings.tokens,
        0,
        kind=UsageKind.MEMORY,
    )
    return [
        f.fact
        for f in await search_facts(session, scope, embeddings.vectors[0], SEARCH_RECALL_LIMIT)
    ]


async def purge_history(
    sessionmaker: async_sessionmaker[AsyncSession], retention_days: int
) -> None:
    async with sessionmaker() as session:
        deleted = await purge_old_messages(session, retention_days)
        await session.commit()
    if deleted:
        log.info("purged %d messages older than %d days", deleted, retention_days)
