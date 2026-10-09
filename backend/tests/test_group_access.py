from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetChatMember

from obot.telegram.group_access import CachedGroupAdmin, group_role


def client_with(status: str | None, *, is_member: bool = True, error: bool = False):
    client = AsyncMock()
    if error:
        client.get_chat_member.side_effect = TelegramBadRequest(
            GetChatMember(chat_id=-1, user_id=1), "chat not found"
        )
    else:
        client.get_chat_member.return_value = SimpleNamespace(status=status, is_member=is_member)
    return client


async def test_group_role_by_status() -> None:
    admin = await group_role(client_with(ChatMemberStatus.ADMINISTRATOR), -1, 1)
    creator = await group_role(client_with(ChatMemberStatus.CREATOR), -1, 1)
    member = await group_role(client_with(ChatMemberStatus.MEMBER), -1, 1)
    restricted = await group_role(client_with(ChatMemberStatus.RESTRICTED), -1, 1)
    assert admin.is_admin and creator.is_admin
    assert member is not None and not member.is_admin
    assert restricted is not None and not restricted.is_admin
    for gone in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
        assert await group_role(client_with(gone), -1, 1) is None
    left_restricted = client_with(ChatMemberStatus.RESTRICTED, is_member=False)
    assert await group_role(left_restricted, -1, 1) is None


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}
        self.ttl: dict[str, int] = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ex=None):
        self.data[key] = str(value).encode()
        self.ttl[key] = ex


async def test_admin_answer_is_cached_for_five_minutes() -> None:
    redis = FakeRedis()
    client = client_with(ChatMemberStatus.ADMINISTRATOR)
    check = CachedGroupAdmin(redis, client, 7, -100, 2002)

    assert await check() is True
    assert await check() is True
    client.get_chat_member.assert_awaited_once_with(-100, 2002)
    assert redis.ttl == {"group-admin:7:-100:2002": 300}

    member = CachedGroupAdmin(redis, client_with(ChatMemberStatus.MEMBER), 7, -100, 3003)
    assert await member() is False
    assert redis.data["group-admin:7:-100:3003"] == b"0"


async def test_telegram_errors_mean_not_admin_and_are_not_cached() -> None:
    redis = FakeRedis()
    check = CachedGroupAdmin(redis, client_with(None, error=True), 7, -100, 2002)
    assert await check() is False
    assert redis.data == {}
