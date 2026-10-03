"""Chat memory repository. Like chat history, everything is scoped by ChatScope."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from obot.chat.history import ChatScope
from obot.db.models import Bot, BotStatus, ChatMemory, MemoryCursor, Message, User

MEMORY_TOOL = "memory"


async def list_facts(session: AsyncSession, scope: ChatScope, limit: int = 200) -> list[ChatMemory]:
    rows = await session.scalars(
        select(ChatMemory)
        .where(ChatMemory.bot_id == scope.bot_id, ChatMemory.tg_chat_id == scope.tg_chat_id)
        .order_by(ChatMemory.id)
        .limit(limit)
    )
    return list(rows)


async def count_facts(session: AsyncSession, scope: ChatScope) -> int:
    return await session.scalar(
        select(func.count()).where(
            ChatMemory.bot_id == scope.bot_id, ChatMemory.tg_chat_id == scope.tg_chat_id
        )
    )


async def search_facts(
    session: AsyncSession, scope: ChatScope, embedding: Sequence[float], limit: int
) -> list[ChatMemory]:
    rows = await session.scalars(
        select(ChatMemory)
        .where(ChatMemory.bot_id == scope.bot_id, ChatMemory.tg_chat_id == scope.tg_chat_id)
        .order_by(ChatMemory.embedding.cosine_distance(embedding))
        .limit(limit)
    )
    return list(rows)


async def add_facts(
    session: AsyncSession,
    scope: ChatScope,
    facts: Sequence[str],
    embeddings: Sequence[Sequence[float]],
) -> None:
    for fact, embedding in zip(facts, embeddings, strict=True):
        session.add(
            ChatMemory(
                bot_id=scope.bot_id,
                tg_chat_id=scope.tg_chat_id,
                fact=fact,
                embedding=list(embedding),
            )
        )
    await session.flush()


async def remove_facts(session: AsyncSession, scope: ChatScope, ids: Sequence[int]) -> None:
    if not ids:
        return
    # Filtering by scope too: an id from another chat must never be deletable from here.
    await session.execute(
        delete(ChatMemory).where(
            ChatMemory.bot_id == scope.bot_id,
            ChatMemory.tg_chat_id == scope.tg_chat_id,
            ChatMemory.id.in_(ids),
        )
    )


async def forget_chat(session: AsyncSession, scope: ChatScope) -> None:
    """Delete everything the bot knows about one chat: facts, raw history and cursor."""
    for model in (ChatMemory, Message, MemoryCursor):
        await session.execute(
            delete(model).where(model.bot_id == scope.bot_id, model.tg_chat_id == scope.tg_chat_id)
        )


async def get_cursor(session: AsyncSession, scope: ChatScope) -> int:
    cursor = await session.get(MemoryCursor, (scope.bot_id, scope.tg_chat_id))
    return cursor.last_message_id if cursor else 0


async def set_cursor(session: AsyncSession, scope: ChatScope, last_message_id: int) -> None:
    await session.execute(
        insert(MemoryCursor)
        .values(bot_id=scope.bot_id, tg_chat_id=scope.tg_chat_id, last_message_id=last_message_id)
        .on_conflict_do_update(
            index_elements=[MemoryCursor.bot_id, MemoryCursor.tg_chat_id],
            set_={"last_message_id": last_message_id},
        )
    )


async def scopes_with_new_messages(session: AsyncSession) -> list[ChatScope]:
    """Chats of active bots with the memory tool that have messages after their cursor."""
    rows = await session.execute(
        select(Message.bot_id, Message.tg_chat_id)
        .join(Bot, Bot.id == Message.bot_id)
        .join(User, User.id == Bot.owner_id)
        .outerjoin(
            MemoryCursor,
            (MemoryCursor.bot_id == Message.bot_id)
            & (MemoryCursor.tg_chat_id == Message.tg_chat_id),
        )
        .where(
            Bot.status == BotStatus.ACTIVE,
            User.is_disabled.is_(False),
            Bot.tools.contains([MEMORY_TOOL]),
            Message.id > func.coalesce(MemoryCursor.last_message_id, 0),
        )
        .group_by(Message.bot_id, Message.tg_chat_id)
    )
    return [ChatScope(bot_id=b, tg_chat_id=c) for b, c in rows]


async def messages_after(
    session: AsyncSession, scope: ChatScope, after_id: int, limit: int
) -> list[Message]:
    rows = await session.scalars(
        select(Message)
        .where(
            Message.bot_id == scope.bot_id,
            Message.tg_chat_id == scope.tg_chat_id,
            Message.id > after_id,
        )
        .order_by(Message.id)
        .limit(limit)
    )
    return list(rows)


async def purge_old_messages(session: AsyncSession, retention_days: int) -> int:
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    result = await session.execute(delete(Message).where(Message.created_at < cutoff))
    return result.rowcount
