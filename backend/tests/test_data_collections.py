from pathlib import Path

from obot.billing.plans import Pricing
from obot.db.models import Collection, CollectionScope

PLANS_FILE = Path(__file__).parent.parent / "plans.yaml"


def test_plans_have_data_limits() -> None:
    pricing = Pricing.load(PLANS_FILE)
    for name in ("free", "paid"):
        plan = pricing.plan(name)
        assert plan.max_data_collections > 0
        assert plan.max_data_records > 0
        assert plan.max_record_bytes > 0


async def test_collection_table_exists(session, cipher) -> None:
    from obot.bots.service import ManagedBotInfo, register_managed_bot

    async def fetch_token(_: int) -> str:
        return "t"

    bot = (
        await register_managed_bot(
            session, cipher, fetch_token, 1001, ManagedBotInfo(1, "b1", "B"), 10
        )
    ).bot
    session.add(
        Collection(
            bot_id=bot.id,
            key="expenses",
            title={"en": "Expenses"},
            scope=CollectionScope.PERSONAL,
            schema={"fields": []},
            rules={},
        )
    )
    await session.flush()
