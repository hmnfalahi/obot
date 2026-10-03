from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from obot.bots.service import (
    ManagedBotInfo,
    RegisterOutcome,
    get_bot_by_tg_id,
    list_active_bots,
    mark_owner_started,
    register_managed_bot,
)
from obot.crypto import TokenCipher
from obot.db.models import Bot, BotStatus

CREATOR = 1001
INFO = ManagedBotInfo(tg_bot_id=555, username="my_bot", display_name="My Bot")


class FakeTelegram:
    def __init__(self, token: str) -> None:
        self.token = token
        self.calls = 0

    async def fetch_token(self, tg_bot_id: int) -> str:
        self.calls += 1
        return self.token


async def register(
    session: AsyncSession, cipher: TokenCipher, tg: FakeTelegram, creator: int = CREATOR
):
    return await register_managed_bot(session, cipher, tg.fetch_token, creator, INFO)


async def test_creates_bot_with_encrypted_token(session, cipher) -> None:
    result = await register(session, cipher, FakeTelegram("555:aaa"))

    assert result.outcome == RegisterOutcome.CREATED
    assert result.token == "555:aaa"
    bot = await get_bot_by_tg_id(session, 555)
    assert bot.owner.tg_user_id == CREATOR
    assert bot.status == BotStatus.ACTIVE
    assert not bot.owner_started
    assert b"aaa" not in bot.token_encrypted
    assert cipher.decrypt(bot.token_encrypted, "555") == "555:aaa"


async def test_repeated_update_is_idempotent(session, cipher) -> None:
    tg = FakeTelegram("555:aaa")
    await register(session, cipher, tg)

    result = await register(session, cipher, tg)

    assert result.outcome == RegisterOutcome.UNCHANGED
    assert await session.scalar(select(func.count()).select_from(Bot)) == 1


async def test_token_replacement_updates_stored_token(session, cipher) -> None:
    await register(session, cipher, FakeTelegram("555:aaa"))

    result = await register(session, cipher, FakeTelegram("555:bbb"))

    assert result.outcome == RegisterOutcome.TOKEN_UPDATED
    assert result.token == "555:bbb"
    assert cipher.decrypt(result.bot.token_encrypted, "555") == "555:bbb"


async def test_disabled_bot_is_not_revived(session, cipher) -> None:
    created = await register(session, cipher, FakeTelegram("555:aaa"))
    created.bot.status = BotStatus.DISABLED
    await session.flush()
    tg = FakeTelegram("555:bbb")

    result = await register(session, cipher, tg)

    assert result.outcome == RegisterOutcome.IGNORED_DISABLED
    assert result.token is None
    assert tg.calls == 0
    assert result.bot.status == BotStatus.DISABLED


async def test_owner_change(session, cipher) -> None:
    created = await register(session, cipher, FakeTelegram("555:aaa"))
    await mark_owner_started(session, created.bot, CREATOR)

    result = await register(session, cipher, FakeTelegram("555:aaa"), creator=2002)

    assert result.outcome == RegisterOutcome.OWNER_CHANGED
    assert result.bot.owner.tg_user_id == 2002
    assert not result.bot.owner_started


async def test_mark_owner_started_only_for_owner(session, cipher) -> None:
    bot = (await register(session, cipher, FakeTelegram("555:aaa"))).bot

    assert not await mark_owner_started(session, bot, tg_user_id=9999)
    assert await mark_owner_started(session, bot, tg_user_id=CREATOR)
    assert not await mark_owner_started(session, bot, tg_user_id=CREATOR)
    assert bot.owner_started


async def test_list_active_bots_excludes_disabled(session, cipher) -> None:
    bot = (await register(session, cipher, FakeTelegram("555:aaa"))).bot
    assert [b.tg_bot_id for b in await list_active_bots(session)] == [555]

    bot.owner.is_disabled = True
    await session.flush()
    assert await list_active_bots(session) == []

    bot.owner.is_disabled = False
    bot.status = BotStatus.DISABLED
    await session.flush()
    assert await list_active_bots(session) == []
