from obot.bots.service import (
    ManagedBotInfo,
    RegisterOutcome,
    get_or_create_user,
    list_active_bots,
    register_managed_bot,
)
from obot.config import Settings
from obot.db.models import BotStatus, ReportStatus
from obot.moderation.service import (
    ban_user,
    create_report,
    disable_bot,
    enable_bot,
    find_bot,
    open_reports,
    resolve_reports,
    unban_user,
)

OWNER = 1001


class FakeTelegram:
    """Plays Telegram's token store: replace_token revokes and issues a new token."""

    def __init__(self) -> None:
        self.tokens: dict[int, str] = {}
        self.replaced: list[int] = []

    async def fetch(self, tg_bot_id: int) -> str:
        return self.tokens.setdefault(tg_bot_id, f"{tg_bot_id}:v1")

    async def replace(self, tg_bot_id: int) -> str:
        self.replaced.append(tg_bot_id)
        self.tokens[tg_bot_id] = f"{tg_bot_id}:v{len(self.replaced) + 1}"
        return self.tokens[tg_bot_id]


async def make_bot(session, cipher, tg, tg_bot_id: int, username: str, owner: int = OWNER):
    result = await register_managed_bot(
        session, cipher, tg.fetch, owner, ManagedBotInfo(tg_bot_id, username, "B"), 10
    )
    return result.bot


async def test_find_bot_is_exact_despite_underscores(session, cipher) -> None:
    tg = FakeTelegram()
    await make_bot(session, cipher, tg, 1, "a_b_bot")
    await make_bot(session, cipher, tg, 2, "axb_bot")

    assert (await find_bot(session, "@A_B_bot")).tg_bot_id == 1
    assert (await find_bot(session, "axb_bot")).tg_bot_id == 2
    assert await find_bot(session, "a%") is None


async def test_kill_switch_revokes_token_and_survives_telegram_update(session, cipher) -> None:
    tg = FakeTelegram()
    bot = await make_bot(session, cipher, tg, 1, "x_bot")

    await disable_bot(session, cipher, tg.replace, bot)

    assert bot.status == BotStatus.DISABLED
    assert tg.replaced == [1]
    assert cipher.decrypt(bot.token_encrypted, "1") == "1:v2"
    assert await list_active_bots(session) == []
    # Telegram then sends managed_bot for the token change: it must not revive the bot.
    again = await register_managed_bot(
        session, cipher, tg.fetch, OWNER, ManagedBotInfo(1, "x_bot", "B"), 10
    )
    assert again.outcome == RegisterOutcome.IGNORED_DISABLED
    assert not again.should_run


async def test_enable_respects_plan_limit(session, cipher) -> None:
    tg = FakeTelegram()
    first = await make_bot(session, cipher, tg, 1, "a_bot")
    second = await make_bot(session, cipher, tg, 2, "b_bot")
    await disable_bot(session, cipher, tg.replace, first)

    assert await enable_bot(session, first, max_bots=1) == BotStatus.SUSPENDED
    await disable_bot(session, cipher, tg.replace, second)
    first.status = BotStatus.DISABLED
    assert await enable_bot(session, first, max_bots=1) == BotStatus.ACTIVE


async def test_ban_disables_all_bots_and_unban_keeps_them_off(session, cipher) -> None:
    tg = FakeTelegram()
    await make_bot(session, cipher, tg, 1, "a_bot")
    await make_bot(session, cipher, tg, 2, "b_bot")
    other = await make_bot(session, cipher, tg, 3, "c_bot", owner=2002)
    owner = await get_or_create_user(session, OWNER)

    killed = await ban_user(session, cipher, tg.replace, owner)

    assert sorted(b.tg_bot_id for b in killed) == [1, 2]
    assert owner.is_disabled
    assert [b.id for b in await list_active_bots(session)] == [other.id]

    await unban_user(session, owner)
    assert not owner.is_disabled
    assert [b.id for b in await list_active_bots(session)] == [other.id]


async def test_reports_lifecycle(session, cipher) -> None:
    tg = FakeTelegram()
    bot = await make_bot(session, cipher, tg, 1, "a_bot")
    await create_report(session, bot, 555, -100, "  spam " + "x" * 2000)
    await create_report(session, bot, 556, None, "scam")

    reports = await open_reports(session)
    assert len(reports) == 2
    assert reports[0].reason.startswith("spam") and len(reports[0].reason) == 1000

    assert await resolve_reports(session, bot, ReportStatus.ACTIONED) == 2
    assert await open_reports(session) == []
    assert reports[0].resolved_at is not None


def test_admin_ids_are_parsed() -> None:
    assert Settings(admin_tg_ids="1, 2,3").admins == frozenset({1, 2, 3})
    assert Settings(admin_tg_ids="").admins == frozenset()


async def test_disabled_status_is_committed_before_token_is_replaced(session, cipher) -> None:
    """Telegram's managed_bot update for the new token may be handled concurrently."""
    from sqlalchemy.ext.asyncio import AsyncSession

    tg = FakeTelegram()
    bot = await make_bot(session, cipher, tg, 1, "x_bot")
    await session.commit()
    seen_by_other_session = []

    async def replace_and_peek(tg_bot_id: int) -> str:
        async with AsyncSession(session.bind) as other:
            seen_by_other_session.append((await find_bot(other, "x_bot")).status)
        return await tg.replace(tg_bot_id)

    await disable_bot(session, cipher, replace_and_peek, bot)

    assert seen_by_other_session == [BotStatus.DISABLED]
