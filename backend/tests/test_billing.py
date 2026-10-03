from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from obot.billing.plans import Pricing
from obot.billing.usage import (
    QuotaReason,
    UsageMeter,
    allowed_tier,
    check_llm_quota,
    record_llm_usage,
    record_tool_usage,
    start_of_day,
)
from obot.bots.service import ManagedBotInfo, register_managed_bot
from obot.db.models import UsageKind

PLANS_FILE = Path(__file__).parent.parent / "plans.yaml"
MODEL = "google/gemini-3.1-flash-lite"


@pytest.fixture
def pricing() -> Pricing:
    return Pricing.load(PLANS_FILE)


async def make_bot(session, cipher, tg_bot_id: int = 1, creator: int = 1001):
    async def fetch_token(_: int) -> str:
        return "t"

    result = await register_managed_bot(
        session, cipher, fetch_token, creator, ManagedBotInfo(tg_bot_id, "b", "B"), 10
    )
    return result.bot


def test_shipped_plans_file_is_valid(pricing) -> None:
    assert pricing.plan("free").daily_llm_messages == 30
    assert pricing.plan("unknown") == pricing.plan("free")
    pricing.require_models([MODEL])
    with pytest.raises(ValueError):
        pricing.require_models(["nope/model"])


def test_cost_and_credits(pricing) -> None:
    # 3000 in * 0.25 + 300 out * 1.5 per million tokens
    cost = pricing.cost_usd(MODEL, 3000, 300)

    assert cost == Decimal("0.0012")
    # 0.0012 * 2.5 markup / 0.0001 per credit
    assert pricing.credits_for(cost) == 30
    assert pricing.credits_for(Decimal("0.0000001")) == 1


def test_start_of_day_uses_owner_timezone() -> None:
    now = datetime(2026, 10, 3, 22, 0, tzinfo=UTC)  # already Oct 4 in Tehran

    assert start_of_day(now, "UTC") == datetime(2026, 10, 3, tzinfo=UTC)
    assert start_of_day(now, "Asia/Tehran").date().isoformat() == "2026-10-04"


async def test_free_plan_daily_message_limit(session, cipher, pricing) -> None:
    pricing.plans["free"].daily_llm_messages = 2
    bot = await make_bot(session, cipher)
    owner = bot.owner
    now = datetime.now(UTC)

    for _ in range(2):
        assert (await check_llm_quota(session, pricing, owner, bot, now)).allowed
        await record_llm_usage(session, pricing, owner, bot, MODEL, 100, 10)

    decision = await check_llm_quota(session, pricing, owner, bot, now)
    assert decision.reason == QuotaReason.DAILY_MESSAGES
    tomorrow = now + timedelta(days=1)
    assert (await check_llm_quota(session, pricing, owner, bot, tomorrow)).allowed


async def test_daily_limit_is_shared_by_all_owner_bots(session, cipher, pricing) -> None:
    pricing.plans["free"].daily_llm_messages = 1
    bot_a = await make_bot(session, cipher, tg_bot_id=1)
    bot_b = await make_bot(session, cipher, tg_bot_id=2)
    await record_llm_usage(session, pricing, bot_a.owner, bot_a, MODEL, 100, 10)

    decision = await check_llm_quota(session, pricing, bot_b.owner, bot_b, datetime.now(UTC))

    assert decision.reason == QuotaReason.DAILY_MESSAGES


async def test_bot_daily_credit_cap(session, cipher, pricing) -> None:
    bot = await make_bot(session, cipher)
    bot.daily_credit_cap = 30
    await record_llm_usage(session, pricing, bot.owner, bot, MODEL, 3000, 300)  # 30 credits

    decision = await check_llm_quota(session, pricing, bot.owner, bot, datetime.now(UTC))

    assert decision.reason == QuotaReason.BOT_DAILY_CAP


async def test_paid_plan_deducts_credits(session, cipher, pricing) -> None:
    bot = await make_bot(session, cipher)
    owner = bot.owner
    owner.plan = "paid"
    owner.credit_balance = 40
    now = datetime.now(UTC)

    event = await record_llm_usage(session, pricing, owner, bot, MODEL, 3000, 300)

    assert event.credits == 30
    assert owner.credit_balance == 10
    assert (await check_llm_quota(session, pricing, owner, bot, now)).allowed
    await record_llm_usage(session, pricing, owner, bot, MODEL, 3000, 300)
    assert owner.credit_balance == -20
    decision = await check_llm_quota(session, pricing, owner, bot, now)
    assert decision.reason == QuotaReason.NO_CREDITS


async def test_free_plan_does_not_deduct_credits(session, cipher, pricing) -> None:
    bot = await make_bot(session, cipher)

    event = await record_llm_usage(session, pricing, bot.owner, bot, MODEL, 3000, 300)

    assert event.credits == 30
    assert bot.owner.credit_balance == 0


async def test_allowed_tier_falls_back_to_plan_tier(session, cipher, pricing) -> None:
    owner = (await make_bot(session, cipher)).owner

    assert allowed_tier(pricing, owner, "strong") == "cheap"
    owner.plan = "paid"
    assert allowed_tier(pricing, owner, "strong") == "strong"


async def test_free_plan_daily_search_limit(session, cipher, pricing) -> None:
    pricing.plans["free"].daily_web_searches = 1
    bot = await make_bot(session, cipher)
    meter = UsageMeter(session, pricing, bot.owner, bot, datetime.now(UTC))

    assert await meter.before("search") is None
    await meter.after("search")

    assert "search limit" in await meter.before("search")
    assert await meter.before("fetch") is None


async def test_search_costs_credits_fetch_is_free(session, cipher, pricing) -> None:
    bot = await make_bot(session, cipher)
    owner = bot.owner
    owner.plan = "paid"
    owner.credit_balance = 1000

    search = await record_tool_usage(session, pricing, owner, bot, UsageKind.SEARCH)
    fetched = await record_tool_usage(session, pricing, owner, bot, UsageKind.FETCH)

    # 0.008 USD * 2.5 markup / 0.0001 per credit
    assert search.credits == 200
    assert fetched.credits == 0
    assert owner.credit_balance == 800
