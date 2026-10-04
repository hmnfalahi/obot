from pathlib import Path

import pytest
from sqlalchemy import func, select

from obot.billing.payments import (
    PAID_PLAN,
    PaymentError,
    find_payment,
    invoice_payload,
    package_for_payload,
    record_purchase,
    record_refund,
)
from obot.billing.plans import Pricing
from obot.bots.service import get_or_create_user
from obot.db.models import Payment
from tests.test_api import ALICE, BOB, auth, make_bot

PLANS_FILE = Path(__file__).parent.parent / "plans.yaml"


@pytest.fixture
def pricing() -> Pricing:
    return Pricing.load(PLANS_FILE)


def test_shipped_packages(pricing) -> None:
    assert [(p.id, p.stars, p.credits) for p in pricing.star_packages] == [
        ("s50", 50, 6500),
        ("s250", 250, 35000),
        ("s1000", 1000, 150000),
    ]


def test_payload_validation(pricing) -> None:
    package = pricing.package("s250")
    payload = invoice_payload(package)

    assert package_for_payload(pricing, payload, "XTR", 250) == package
    with pytest.raises(PaymentError, match="price"):
        package_for_payload(pricing, payload, "XTR", 1)
    with pytest.raises(PaymentError, match="unknown"):
        package_for_payload(pricing, payload, "USD", 250)
    with pytest.raises(PaymentError, match="no longer sold"):
        package_for_payload(pricing, "credits:gone", "XTR", 250)


async def test_purchase_credits_once_and_switches_plan(session, pricing) -> None:
    user = await get_or_create_user(session, 1001)
    package = pricing.package("s50")

    assert await record_purchase(session, user, package, "charge-1")
    # Telegram may deliver the same successful_payment again.
    assert not await record_purchase(session, user, package, "charge-1")

    assert user.credit_balance == 6500
    assert user.plan == PAID_PLAN
    assert await session.scalar(select(func.count()).select_from(Payment)) == 1


async def test_refund_takes_credits_back(session, pricing) -> None:
    user = await get_or_create_user(session, 1001)
    await record_purchase(session, user, pricing.package("s50"), "charge-1")
    user.credit_balance -= 6000  # spent most of it
    await session.flush()

    await record_refund(session, await find_payment(session, "charge-1"))

    assert user.credit_balance == 500 - 6500
    assert (await find_payment(session, "charge-1")).refunded_at is not None


# --- API ------------------------------------------------------------------------------------


async def test_api_lists_packages(api) -> None:
    response = await api.get("/api/billing/packages", headers=auth(ALICE))

    assert [p["id"] for p in response.json()] == ["s50", "s250", "s1000"]


async def test_any_owner_may_recommend_the_strong_model(api) -> None:
    """Only a recommendation: each person gets it if their own plan allows it (SPEC 8.7)."""
    bot_id = await make_bot(api, ALICE, 11)

    free = await api.patch(
        f"/api/bots/{bot_id}", json={"model_tier": "strong"}, headers=auth(ALICE)
    )
    assert free.json()["model_tier"] == "strong"

    async with api.state.sessionmaker() as session:
        user = await get_or_create_user(session, ALICE["id"])
        user.plan = PAID_PLAN
        await session.commit()
    me = await api.get("/api/me", headers=auth(ALICE))
    assert me.json()["model_tiers"] == ["cheap", "strong"]


async def test_invoice_link_for_package(api, monkeypatch) -> None:
    from obot.api import routes

    created = {}

    class FakeBot:
        def __init__(self, token: str) -> None:
            self.session = self

        async def create_invoice_link(self, **kwargs) -> str:
            created.update(kwargs)
            return "https://t.me/$invoice"

        async def close(self) -> None:
            pass

    monkeypatch.setattr(routes, "TelegramBot", FakeBot)

    ok = await api.post("/api/billing/invoice", json={"package_id": "s250"}, headers=auth(BOB))
    missing = await api.post("/api/billing/invoice", json={"package_id": "x"}, headers=auth(BOB))

    assert ok.json() == {"link": "https://t.me/$invoice"}
    assert created["currency"] == "XTR"
    assert created["prices"][0].amount == 250
    assert created["payload"] == "credits:s250"
    assert missing.status_code == 404


async def test_manual_grant_credits_once_and_records_admin(session) -> None:
    from obot.billing.payments import grant_credits

    user = await get_or_create_user(session, 1001)

    assert await grant_credits(session, user, 5000, admin_tg_id=9, grant_id="1001-77", note="r")
    # A double-tapped grant button carries the same receipt id.
    assert not await grant_credits(session, user, 5000, admin_tg_id=9, grant_id="1001-77")

    assert user.credit_balance == 5000 and user.plan == PAID_PLAN
    [payment] = list(await session.scalars(select(Payment)))
    assert (payment.method, payment.granted_by, payment.stars) == ("manual", 9, 0)
    assert payment.telegram_charge_id == "manual-1001-77"


async def test_stars_and_manual_payments_are_told_apart(session, pricing) -> None:
    from obot.billing.payments import grant_credits

    user = await get_or_create_user(session, 1001)
    await record_purchase(session, user, pricing.package("s50"), "tg-charge")
    await grant_credits(session, user, 100, admin_tg_id=9, grant_id="x")

    methods = sorted(p.method for p in await session.scalars(select(Payment)))
    assert methods == ["manual", "stars"]
    assert user.credit_balance == 6600
