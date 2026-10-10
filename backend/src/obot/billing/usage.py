"""Usage metering and quota checks.

Every call is paid by its payer: whoever started the request (SPEC 8.7), not
the bot's owner. Credits are computed for every call, also on the free plan, so
the daily limits work the same for everyone; they are only deducted on plans with
`uses_credits`. Replies on the payer's own key (BYOK) are free and do not count
as messages; their tool calls are metered as usual. A payer of None means Obot
pays (background work nobody asked for, like memory in groups).
"""

import asyncio
import enum
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from obot.billing.plans import Pricing
from obot.db.models import Bot, UsageEvent, UsageKind, User


class QuotaReason(enum.StrEnum):
    DAILY_MESSAGES = "daily_messages"
    DAILY_SEARCHES = "daily_searches"
    NO_CREDITS = "no_credits"


@dataclass(frozen=True)
class QuotaDecision:
    reason: QuotaReason | None = None

    @property
    def allowed(self) -> bool:
        return self.reason is None


def start_of_day(now: datetime, timezone: str) -> datetime:
    local = now.astimezone(ZoneInfo(timezone))
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def allowed_tier(pricing: Pricing, payer: User, requested: str) -> str:
    tiers = pricing.plan(payer.plan).model_tiers
    return requested if requested in tiers else tiers[0]


async def check_llm_quota(
    session: AsyncSession,
    pricing: Pricing,
    payer: User,
    now: datetime,
    *,
    byok: bool = False,
) -> QuotaDecision:
    """May `payer` have the bot make a model call? `byok`: it goes to the payer's own key."""
    plan = pricing.plan(payer.plan)
    since = start_of_day(now, payer.timezone)

    if plan.daily_llm_messages is not None and not byok:
        used = await count_messages(session, payer, since)
        if used >= plan.daily_llm_messages:
            return QuotaDecision(QuotaReason.DAILY_MESSAGES)

    if plan.uses_credits and payer.credit_balance <= 0 and not byok:
        return QuotaDecision(QuotaReason.NO_CREDITS)

    return QuotaDecision()


async def count_messages(session: AsyncSession, payer: User, since: datetime) -> int:
    """Replies since `since` that count against the plan's daily message limit."""
    return await session.scalar(
        select(func.count()).where(
            UsageEvent.payer_id == payer.id,
            UsageEvent.kind == UsageKind.LLM,
            UsageEvent.byok.is_(False),
            UsageEvent.created_at >= since,
        )
    )


async def record_llm_usage(
    session: AsyncSession,
    pricing: Pricing,
    payer: User | None,
    bot: Bot,
    model: str,
    tokens_in: int,
    tokens_out: int,
    kind: UsageKind = UsageKind.LLM,
    *,
    byok: bool = False,
) -> UsageEvent:
    """Record a model call; MEMORY calls cost credits but do not count as messages.

    `byok` calls are paid by the payer at their provider: recorded for statistics, free here.
    """
    event = UsageEvent(
        payer_id=payer.id if payer else None,
        bot_id=bot.id,
        kind=kind,
        model=model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=0 if byok else pricing.cost_usd(model, tokens_in, tokens_out),
        byok=byok,
    )
    return await _record(session, pricing, payer, event)


async def record_tool_usage(
    session: AsyncSession, pricing: Pricing, payer: User, bot: Bot, kind: UsageKind
) -> UsageEvent:
    event = UsageEvent(
        payer_id=payer.id, bot_id=bot.id, kind=kind, cost_usd=pricing.tools[kind.value]
    )
    return await _record(session, pricing, payer, event)


async def _record(
    session: AsyncSession, pricing: Pricing, payer: User | None, event: UsageEvent
) -> UsageEvent:
    event.credits = pricing.credits_for(event.cost_usd) if event.cost_usd else 0
    session.add(event)
    if payer is not None and pricing.plan(payer.plan).uses_credits and event.credits:
        # Atomic decrement so concurrent replies cannot overwrite each other;
        # the ORM synchronizes `payer.credit_balance` in the session.
        await session.execute(
            update(User)
            .where(User.id == payer.id)
            .values(credit_balance=User.credit_balance - event.credits)
        )
    await session.flush()
    return event


async def check_tool_quota(
    session: AsyncSession, pricing: Pricing, payer: User, kind: UsageKind, now: datetime
) -> QuotaReason | None:
    plan = pricing.plan(payer.plan)
    if kind == UsageKind.SEARCH:
        used = await session.scalar(
            select(func.count()).where(
                UsageEvent.payer_id == payer.id,
                UsageEvent.kind == UsageKind.SEARCH,
                UsageEvent.created_at >= start_of_day(now, payer.timezone),
            )
        )
        if used >= plan.daily_web_searches:
            return QuotaReason.DAILY_SEARCHES
    if plan.uses_credits and pricing.tools[kind.value] and payer.credit_balance <= 0:
        return QuotaReason.NO_CREDITS
    return None


TOOL_REFUSALS = {
    QuotaReason.DAILY_SEARCHES: "the user's daily web search limit is reached",
    QuotaReason.NO_CREDITS: "the user is out of credits",
}


class UsageMeter:
    """ToolMeter backed by the database.

    Tool calls of one reply may run concurrently, but an AsyncSession must not be
    used concurrently, so every access is serialized.
    """

    def __init__(
        self, session: AsyncSession, pricing: Pricing, payer: User, bot: Bot, now: datetime
    ) -> None:
        self._session = session
        self._pricing = pricing
        self._payer = payer
        self._bot = bot
        self._now = now
        self._lock = asyncio.Lock()

    async def before(self, kind: str) -> str | None:
        async with self._lock:
            reason = await check_tool_quota(
                self._session, self._pricing, self._payer, UsageKind(kind), self._now
            )
        return TOOL_REFUSALS[reason] if reason else None

    async def after(self, kind: str) -> None:
        async with self._lock:
            await record_tool_usage(
                self._session, self._pricing, self._payer, self._bot, UsageKind(kind)
            )
