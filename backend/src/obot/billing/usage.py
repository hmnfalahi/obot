"""Usage metering and quota checks.

Credits are computed for every call, also on the free plan, so per-bot daily
caps work the same for everyone; they are only deducted on plans with
`uses_credits`. Replies on the owner's own key (BYOK) are free and do not
count as messages; their tool calls are metered as usual.
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
    BOT_DAILY_CAP = "bot_daily_cap"
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


def allowed_tier(pricing: Pricing, owner: User, requested: str) -> str:
    tiers = pricing.plan(owner.plan).model_tiers
    return requested if requested in tiers else tiers[0]


async def check_llm_quota(
    session: AsyncSession,
    pricing: Pricing,
    owner: User,
    bot: Bot,
    now: datetime,
    *,
    byok: bool = False,
) -> QuotaDecision:
    """May the bot make a model call? `byok`: the call goes to the owner's own key."""
    plan = pricing.plan(owner.plan)
    since = start_of_day(now, owner.timezone)

    if plan.daily_llm_messages is not None and not byok:
        used = await count_messages(session, owner, since)
        if used >= plan.daily_llm_messages:
            return QuotaDecision(QuotaReason.DAILY_MESSAGES)

    if bot.daily_credit_cap is not None:
        spent = await session.scalar(
            select(func.coalesce(func.sum(UsageEvent.credits), 0)).where(
                UsageEvent.bot_id == bot.id, UsageEvent.created_at >= since
            )
        )
        if spent >= bot.daily_credit_cap:
            return QuotaDecision(QuotaReason.BOT_DAILY_CAP)

    if plan.uses_credits and owner.credit_balance <= 0 and not byok:
        return QuotaDecision(QuotaReason.NO_CREDITS)

    return QuotaDecision()


async def count_messages(session: AsyncSession, owner: User, since: datetime) -> int:
    """Replies since `since` that count against the plan's daily message limit."""
    return await session.scalar(
        select(func.count()).where(
            UsageEvent.owner_id == owner.id,
            UsageEvent.kind == UsageKind.LLM,
            UsageEvent.byok.is_(False),
            UsageEvent.created_at >= since,
        )
    )


async def record_llm_usage(
    session: AsyncSession,
    pricing: Pricing,
    owner: User,
    bot: Bot,
    model: str,
    tokens_in: int,
    tokens_out: int,
    kind: UsageKind = UsageKind.LLM,
    *,
    byok: bool = False,
) -> UsageEvent:
    """Record a model call; MEMORY calls cost credits but do not count as messages.

    `byok` calls are paid by the owner at their provider: recorded for statistics, free here.
    """
    event = UsageEvent(
        owner_id=owner.id,
        bot_id=bot.id,
        kind=kind,
        model=model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=0 if byok else pricing.cost_usd(model, tokens_in, tokens_out),
        byok=byok,
    )
    return await _record(session, pricing, owner, event)


async def record_tool_usage(
    session: AsyncSession, pricing: Pricing, owner: User, bot: Bot, kind: UsageKind
) -> UsageEvent:
    event = UsageEvent(
        owner_id=owner.id, bot_id=bot.id, kind=kind, cost_usd=pricing.tools[kind.value]
    )
    return await _record(session, pricing, owner, event)


async def _record(
    session: AsyncSession, pricing: Pricing, owner: User, event: UsageEvent
) -> UsageEvent:
    event.credits = pricing.credits_for(event.cost_usd) if event.cost_usd else 0
    session.add(event)
    if pricing.plan(owner.plan).uses_credits and event.credits:
        # Atomic decrement so concurrent replies cannot overwrite each other;
        # the ORM synchronizes `owner.credit_balance` in the session.
        await session.execute(
            update(User)
            .where(User.id == owner.id)
            .values(credit_balance=User.credit_balance - event.credits)
        )
    await session.flush()
    return event


async def check_tool_quota(
    session: AsyncSession, pricing: Pricing, owner: User, kind: UsageKind, now: datetime
) -> QuotaReason | None:
    plan = pricing.plan(owner.plan)
    if kind == UsageKind.SEARCH:
        used = await session.scalar(
            select(func.count()).where(
                UsageEvent.owner_id == owner.id,
                UsageEvent.kind == UsageKind.SEARCH,
                UsageEvent.created_at >= start_of_day(now, owner.timezone),
            )
        )
        if used >= plan.daily_web_searches:
            return QuotaReason.DAILY_SEARCHES
    if plan.uses_credits and pricing.tools[kind.value] and owner.credit_balance <= 0:
        return QuotaReason.NO_CREDITS
    return None


TOOL_REFUSALS = {
    QuotaReason.DAILY_SEARCHES: "the owner's daily web search limit is reached",
    QuotaReason.NO_CREDITS: "the owner is out of credits",
}


class UsageMeter:
    """ToolMeter backed by the database.

    Tool calls of one reply may run concurrently, but an AsyncSession must not be
    used concurrently, so every access is serialized.
    """

    def __init__(
        self, session: AsyncSession, pricing: Pricing, owner: User, bot: Bot, now: datetime
    ) -> None:
        self._session = session
        self._pricing = pricing
        self._owner = owner
        self._bot = bot
        self._now = now
        self._lock = asyncio.Lock()

    async def before(self, kind: str) -> str | None:
        async with self._lock:
            reason = await check_tool_quota(
                self._session, self._pricing, self._owner, UsageKind(kind), self._now
            )
        return TOOL_REFUSALS[reason] if reason else None

    async def after(self, kind: str) -> None:
        async with self._lock:
            await record_tool_usage(
                self._session, self._pricing, self._owner, self._bot, UsageKind(kind)
            )
