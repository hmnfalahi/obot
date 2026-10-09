from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from obot.db.models import BotChat


async def get_membership(session: AsyncSession, bot_id: int, tg_chat_id: int) -> BotChat | None:
    return await session.scalar(
        select(BotChat).where(BotChat.bot_id == bot_id, BotChat.tg_chat_id == tg_chat_id)
    )


async def upsert_membership(
    session: AsyncSession,
    bot_id: int,
    tg_chat_id: int,
    *,
    chat_type: str,
    title: str | None,
    is_member: bool,
    is_admin: bool,
) -> BotChat:
    row = await session.scalar(
        select(BotChat).where(BotChat.bot_id == bot_id, BotChat.tg_chat_id == tg_chat_id)
    )
    if row is None:
        row = BotChat(bot_id=bot_id, tg_chat_id=tg_chat_id)
        session.add(row)
    elif is_member and not row.is_member:
        row.joined_at = datetime.now(UTC)
    row.chat_type = chat_type
    row.title = title
    row.is_admin = is_member and is_admin
    if row.is_member and not is_member:
        row.left_at = datetime.now(UTC)
    row.is_member = is_member
    await session.flush()
    return row


async def list_memberships(session: AsyncSession, bot_id: int) -> list[BotChat]:
    rows = await session.scalars(
        select(BotChat).where(BotChat.bot_id == bot_id, BotChat.is_member.is_(True))
    )
    return list(rows)
