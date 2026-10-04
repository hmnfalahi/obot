"""Credit purchases with Telegram Stars in the factory bot."""

import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from sqlalchemy.ext.asyncio import AsyncSession

from obot.billing.payments import (
    STARS_CURRENCY,
    PaymentError,
    find_payment,
    invoice_payload,
    package_for_payload,
    record_purchase,
    record_refund,
)
from obot.billing.plans import StarPackage
from obot.bots.service import get_or_create_user
from obot.db.models import User
from obot.i18n import lang_of, t
from obot.telegram.moderation import IsAdmin
from obot.telegram.runtime import Runtime

log = logging.getLogger(__name__)

router = Router(name="payments")


def invoice_text(lang: str, package: StarPackage) -> tuple[str, str]:
    title = t(lang, "buy.title", credits=f"{package.credits:,}")
    return title, t(lang, "buy.description", credits=f"{package.credits:,}")


def invoice_args(lang: str, package: StarPackage) -> dict:
    """Arguments shared by send_invoice (chat) and create_invoice_link (mini app)."""
    title, description = invoice_text(lang, package)
    return {
        "title": title,
        "description": description,
        "payload": invoice_payload(package),
        "currency": STARS_CURRENCY,
        "prices": [LabeledPrice(label=title, amount=package.stars)],
    }


@router.message(Command("buy"), F.chat.type == ChatType.PRIVATE)
async def on_buy(message: Message, session: AsyncSession, runtime: Runtime) -> None:
    user = await get_or_create_user(session, message.from_user.id, message.from_user.language_code)
    lang = lang_of(message.from_user.language_code)
    buttons = [
        [
            InlineKeyboardButton(
                text=t(lang, "buy.button", stars=p.stars, credits=f"{p.credits:,}"),
                callback_data=f"buy:{p.id}",
            )
        ]
        for p in runtime.pricing.star_packages
    ]
    await message.answer(
        t(lang, "buy.menu", balance=f"{user.credit_balance:,}"),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data.startswith("buy:"))
async def on_buy_package(query: CallbackQuery, bot: Bot, runtime: Runtime) -> None:
    package = runtime.pricing.package(query.data.removeprefix("buy:"))
    if package is None:
        await query.answer(t(lang_of(query.from_user.language_code), "factory.stale"))
        return
    await query.answer()
    await bot.send_invoice(
        query.from_user.id, **invoice_args(lang_of(query.from_user.language_code), package)
    )


@router.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery, session: AsyncSession, runtime: Runtime) -> None:
    """Telegram asks us to confirm within 10 seconds before charging."""
    try:
        package_for_payload(
            runtime.pricing, query.invoice_payload, query.currency, query.total_amount
        )
        user = await get_or_create_user(session, query.from_user.id, query.from_user.language_code)
        if user.is_disabled:
            raise PaymentError("account suspended")
    except PaymentError as e:
        await query.answer(ok=False, error_message=str(e))
        return
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def on_paid(message: Message, session: AsyncSession, runtime: Runtime) -> None:
    payment = message.successful_payment
    lang = lang_of(message.from_user.language_code)
    user = await get_or_create_user(session, message.from_user.id, message.from_user.language_code)
    try:
        package = package_for_payload(
            runtime.pricing, payment.invoice_payload, payment.currency, payment.total_amount
        )
    except PaymentError:
        # Charged but not recognised (e.g. the package was removed meanwhile): flag it.
        log.error("unrecognised paid invoice %s from %s", payment.invoice_payload, user.id)
        await _alert_admins(runtime, f"⚠️ Unrecognised payment {payment.telegram_payment_charge_id}")
        await message.answer(t(lang, "factory.error"))
        return
    if await record_purchase(session, user, package, payment.telegram_payment_charge_id):
        log.info("user %s bought %s for %d stars", user.id, package.id, package.stars)
    await message.answer(
        t(lang, "buy.done", credits=f"{package.credits:,}", balance=f"{user.credit_balance:,}")
    )


async def _alert_admins(runtime: Runtime, text: str) -> None:
    for admin in runtime.admins:
        try:
            await runtime.factory_bot.send_message(admin, text)
        except Exception:
            log.warning("could not alert admin %s", admin, exc_info=True)


@router.message(Command("refund"), IsAdmin(), F.chat.type == ChatType.PRIVATE)
async def on_refund(
    message: Message, command: CommandObject, bot: Bot, session: AsyncSession
) -> None:
    charge_id = (command.args or "").strip()
    payment = await find_payment(session, charge_id) if charge_id else None
    if payment is None:
        await message.answer("No such payment. Usage: /refund <telegram charge id>")
        return
    if payment.refunded_at is not None:
        await message.answer("Already refunded.")
        return
    user = await session.get(User, payment.user_id)
    await bot.refund_star_payment(user_id=user.tg_user_id, telegram_payment_charge_id=charge_id)
    await record_refund(session, payment)
    await message.answer(
        f"Refunded {payment.stars} ⭐ to {user.tg_user_id}; {payment.credits} credits removed."
    )
