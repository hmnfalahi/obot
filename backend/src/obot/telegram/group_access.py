"""Who is in a group and who manages it, as Telegram says (SPEC 8.8 group data)."""

from dataclasses import dataclass

from aiogram import Bot as Client
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError

ADMIN_STATUSES = (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
ADMIN_CACHE_SECONDS = 300


@dataclass(frozen=True)
class GroupRole:
    is_admin: bool


async def group_role(client: Client, chat_id: int, tg_user_id: int) -> GroupRole | None:
    """The person's role in the group now; None if they are not in it. Telegram errors
    (TelegramAPIError) are left to the caller."""
    member = await client.get_chat_member(chat_id, tg_user_id)
    if member.status in ADMIN_STATUSES:
        return GroupRole(is_admin=True)
    if member.status == ChatMemberStatus.MEMBER:
        return GroupRole(is_admin=False)
    if member.status == ChatMemberStatus.RESTRICTED and getattr(member, "is_member", False):
        return GroupRole(is_admin=False)
    return None


class CachedGroupAdmin:
    """Whether a person is an admin of a group, asked of Telegram at most every few minutes
    per bot, group and person. A Telegram error counts as no and is not remembered."""

    def __init__(self, redis, client: Client, bot_id: int, chat_id: int, tg_user_id: int) -> None:
        self._redis, self._client = redis, client
        self._chat_id, self._tg_user_id = chat_id, tg_user_id
        self._key = f"group-admin:{bot_id}:{chat_id}:{tg_user_id}"

    async def __call__(self) -> bool:
        cached = await self._redis.get(self._key)
        if cached is not None:
            return cached in (b"1", "1")
        try:
            role = await group_role(self._client, self._chat_id, self._tg_user_id)
        except TelegramAPIError:
            return False
        admin = role is not None and role.is_admin
        await self._redis.set(self._key, "1" if admin else "0", ex=ADMIN_CACHE_SECONDS)
        return admin
