"""Buying credits with Telegram Stars (SPEC 8.5, P2-1).

Invoice payloads are "credits:<package id>"; the payer is whoever Telegram says
paid, never something taken from the payload.
"""

from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from obot.billing.plans import Pricing, StarPackage
from obot.db.models import Payment, User

STARS_CURRENCY = "XTR"
PAID_PLAN = "paid"
PAYLOAD_PREFIX = "credits:"


class PaymentError(Exception):
    """The purchase cannot go ahead; the message is shown to the payer."""


def invoice_payload(package: StarPackage) -> str:
    return f"{PAYLOAD_PREFIX}{package.id}"


def package_for_payload(pricing: Pricing, payload: str, currency: str, amount: int) -> StarPackage:
    """Validate an invoice at pre-checkout and on payment."""
    if currency != STARS_CURRENCY or not payload.startswith(PAYLOAD_PREFIX):
        raise PaymentError("unknown invoice")
    package = pricing.package(payload.removeprefix(PAYLOAD_PREFIX))
    if package is None:
        raise PaymentError("this package is no longer sold")
    if amount != package.stars:
        raise PaymentError("the price of this package has changed")
    return package


async def record_purchase(
    session: AsyncSession, user: User, package: StarPackage, charge_id: str
) -> bool:
    """Credit a Stars purchase and move the user to the paid plan. False if already recorded."""
    return await _credit(
        session, user, package.id, package.stars, package.credits, charge_id, method="stars"
    )


async def grant_credits(
    session: AsyncSession,
    user: User,
    credits: int,
    admin_tg_id: int,
    grant_id: str,
    package_id: str = "manual",
    note: str | None = None,
) -> bool:
    """Credit a manual (card-to-card) payment. `grant_id` makes a repeated grant a no-op."""
    return await _credit(
        session,
        user,
        package_id,
        0,
        credits,
        f"manual-{grant_id}",
        method="manual",
        granted_by=admin_tg_id,
        note=note,
    )


async def _credit(
    session: AsyncSession,
    user: User,
    package_id: str,
    stars: int,
    credits: int,
    charge_id: str,
    *,
    method: str,
    granted_by: int | None = None,
    note: str | None = None,
) -> bool:
    inserted = await session.scalar(
        insert(Payment)
        .values(
            user_id=user.id,
            method=method,
            granted_by=granted_by,
            note=note,
            package_id=package_id,
            stars=stars,
            credits=credits,
            telegram_charge_id=charge_id,
        )
        .on_conflict_do_nothing(index_elements=[Payment.telegram_charge_id])
        .returning(Payment.id)
    )
    if inserted is None:
        return False
    await session.execute(
        update(User)
        .where(User.id == user.id)
        .values(credit_balance=User.credit_balance + credits, plan=PAID_PLAN)
    )
    await session.flush()
    return True


async def find_payment(session: AsyncSession, charge_id: str) -> Payment | None:
    return await session.scalar(select(Payment).where(Payment.telegram_charge_id == charge_id))


async def record_refund(session: AsyncSession, payment: Payment) -> None:
    """Take the purchased credits back (the balance may go negative) after a refund."""
    payment.refunded_at = datetime.now(UTC)
    await session.execute(
        update(User)
        .where(User.id == payment.user_id)
        .values(credit_balance=User.credit_balance - payment.credits)
    )
    await session.flush()
