"""Chat history repository.

Every read and write takes a ChatScope, so history of one (bot, chat) pair can
never be mixed into another chat's context. Do not query Message directly.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from obot.db.models import Message, SenderKind


@dataclass(frozen=True)
class ChatScope:
    bot_id: int
    tg_chat_id: int


async def record_message(
    session: AsyncSession,
    scope: ChatScope,
    *,
    sender_kind: SenderKind,
    text: str,
    tg_message_id: int | None = None,
    sender_tg_id: int | None = None,
    sender_name: str | None = None,
) -> Message:
    message = Message(
        bot_id=scope.bot_id,
        tg_chat_id=scope.tg_chat_id,
        tg_message_id=tg_message_id,
        sender_kind=sender_kind,
        sender_tg_id=sender_tg_id,
        sender_name=sender_name,
        text=text,
    )
    session.add(message)
    await session.flush()
    return message


async def recent_messages(
    session: AsyncSession, scope: ChatScope, limit: int, before_id: int | None = None
) -> list[Message]:
    """Return up to `limit` latest messages of the scope, oldest first."""
    query = select(Message).where(
        Message.bot_id == scope.bot_id, Message.tg_chat_id == scope.tg_chat_id
    )
    if before_id is not None:
        query = query.where(Message.id < before_id)
    rows = await session.scalars(query.order_by(Message.id.desc()).limit(limit))
    return list(reversed(list(rows)))
