"""Who reads or writes a bot's data, and which records that lets them see (SPEC 8.8).

Every query on records takes a DataSpace; the filters here are the only place the
access rules live. In a group chat only group collections are visible, so personal and
shared data never reach a group.
"""

from dataclasses import dataclass
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement
from sqlalchemy.ext.asyncio import AsyncSession

from obot.db.models import Bot, Collection, CollectionScope, Record, User


@dataclass(frozen=True)
class DataSpace:
    bot_id: int
    owner_id: int
    user_id: int  # the caller
    timezone: str
    owner_timezone: str
    plan: str
    owner_plan: str
    tg_chat_id: int | None = None  # set in a group chat
    is_group_admin: bool = False

    @property
    def is_owner(self) -> bool:
        return self.user_id == self.owner_id


async def space_for(
    session: AsyncSession,
    bot: Bot,
    user: User,
    *,
    tg_chat_id: int | None = None,
    is_group_admin: bool = False,
) -> DataSpace:
    owner = await session.get(User, bot.owner_id)
    return DataSpace(
        bot_id=bot.id,
        owner_id=owner.id,
        user_id=user.id,
        timezone=user.timezone,
        owner_timezone=owner.timezone,
        plan=user.plan,
        owner_plan=owner.plan,
        tg_chat_id=tg_chat_id,
        is_group_admin=is_group_admin,
    )


def visible(space: DataSpace, c: Collection) -> bool:
    if c.bot_id != space.bot_id:
        return False
    if c.created_by_user_id is not None and c.created_by_user_id != space.user_id:
        return False
    return (c.scope == CollectionScope.GROUP) == (space.tg_chat_id is not None)


def row_filter(space: DataSpace, c: Collection) -> list[ColumnElement[bool]]:
    """WHERE clauses selecting the live records of `c` this space may read."""
    clauses = [Record.collection_id == c.id, Record.deleted_at.is_(None)]
    match c.scope:
        case CollectionScope.PERSONAL:
            clauses.append(Record.author_user_id == space.user_id)
        case CollectionScope.SHARED:
            if not space.is_owner:
                clauses.append(Record.author_user_id == space.user_id)
        case CollectionScope.GROUP:
            clauses.append(Record.tg_chat_id == space.tg_chat_id)
    return clauses


def can_change(space: DataSpace, c: Collection, r: Record) -> bool:
    """Whether the space may edit or delete `r`, a record it can already read."""
    if r.author_user_id == space.user_id:
        return True
    match c.scope:
        case CollectionScope.SHARED:
            return space.is_owner
        case CollectionScope.GROUP:
            return space.is_group_admin
    return False


def record_scope_key(c: Collection, r: Record) -> str:
    """Where a record's unique rules apply."""
    match c.scope:
        case CollectionScope.PERSONAL:
            return f"u:{r.author_user_id}"
        case CollectionScope.GROUP:
            return f"c:{r.tg_chat_id}"
    return "all"


def payer(space: DataSpace, c: Collection) -> tuple[int, str]:
    """Whose plan limits a collection's records: (user id, plan name)."""
    if c.scope == CollectionScope.PERSONAL:
        return space.user_id, space.plan
    return space.owner_id, space.owner_plan


def tz_for(space: DataSpace, c: Collection) -> ZoneInfo:
    if c.scope == CollectionScope.PERSONAL:
        return ZoneInfo(space.timezone)
    return ZoneInfo(space.owner_timezone)
