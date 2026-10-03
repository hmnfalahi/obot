"""Usage metering and quota checks.

Credits are computed for every call, also on the free plan, so per-bot daily
caps work the same for everyone; they are only deducted on plans with
`uses_credits`.
"""

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
    session: AsyncSession, pricing: Pricing, owner: User, bot: Bot, now: datetime
) -> QuotaDecision:
    plan = pricing.plan(owner.plan)
    since = start_of_day(now, owner.timezone)

    if plan.daily_llm_messages is not None:
        used = await session.scalar(
            select(func.count()).where(
                UsageEvent.owner_id == owner.id,
                UsageEvent.kind == UsageKind.LLM,
                UsageEvent.created_at >= since,
            )
        )
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

    if plan.uses_credits and owner.credit_balance <= 0:
        return QuotaDecision(QuotaReason.NO_CREDITS)

    return QuotaDecision()


async def record_llm_usage(
    session: AsyncSession,
    pricing: Pricing,
    owner: User,
    bot: Bot,
    model: str,
    tokens_in: int,
    tokens_out: int,
) -> UsageEvent:
    cost = pricing.cost_usd(model, tokens_in, tokens_out)
    event = UsageEvent(
        owner_id=owner.id,
        bot_id=bot.id,
        kind=UsageKind.LLM,
        model=model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost,
        credits=pricing.credits_for(cost),
    )
    session.add(event)
    if pricing.plan(owner.plan).uses_credits:
        # Atomic decrement so concurrent replies cannot overwrite each other;
        # the ORM synchronizes `owner.credit_balance` in the session.
        await session.execute(
            update(User)
            .where(User.id == owner.id)
            .values(credit_balance=User.credit_balance - event.credits)
        )
    await session.flush()
    return event
