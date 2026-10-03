from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic_ai.models.test import TestModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import joinedload

from obot.api import routes
from obot.api.app import create_app
from obot.api.auth import InvalidInitData, sign_init_data, validate_init_data
from obot.api.deps import ApiState
from obot.billing.plans import ModelPrice, Pricing
from obot.billing.usage import record_llm_usage
from obot.bots.service import ManagedBotInfo, register_managed_bot
from obot.db.models import Bot, DraftState, FactoryDraft, Task, TaskStatus
from obot.tasks.planner import PlannedTask
from obot.tasks.service import build_tasks, replace_bot_tasks

TOKEN = "123:factory-secret"
ALICE = {"id": 1001, "first_name": "Alice", "language_code": "fa"}
BOB = {"id": 2002, "first_name": "Bob", "language_code": "en"}
PLANS_FILE = Path(__file__).parent.parent / "plans.yaml"


def auth(user: dict) -> dict[str, str]:
    return {"Authorization": f"tma {sign_init_data(user, TOKEN)}"}


class FakeModels:
    def __init__(self) -> None:
        self.model = TestModel()

    def for_tier(self, tier: str):
        return self.model


@pytest.fixture
async def api(session, cipher, monkeypatch):
    async def no_profile_sync(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr(routes, "sync_profile", no_profile_sync)
    pricing = Pricing.load(PLANS_FILE)
    pricing.models["test"] = ModelPrice(input_per_mtok=1, output_per_mtok=1)
    state = ApiState(
        sessionmaker=async_sessionmaker(session.bind, expire_on_commit=False),
        cipher=cipher,
        pricing=pricing,
        models=FakeModels(),
        factory_token=TOKEN,
        factory_username="obotfactorybot",
    )
    app = create_app(state)
    app.state.api = state
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        client.state = state
        yield client


async def make_bot(api, tg_user: dict, tg_bot_id: int):
    async with api.state.sessionmaker() as session:

        async def fetch_token(_: int) -> str:
            return f"{tg_bot_id}:token"

        result = await register_managed_bot(
            session,
            api.state.cipher,
            fetch_token,
            tg_user["id"],
            ManagedBotInfo(tg_bot_id, f"bot{tg_bot_id}_bot", f"Bot {tg_bot_id}"),
            10,
        )
        result.bot.owner_started = True
        await session.commit()
        return result.bot.id


async def make_task(api, bot_id: int) -> int:
    async with api.state.sessionmaker() as session:
        bot = await session.get(Bot, bot_id, options=[joinedload(Bot.owner)])
        planned = PlannedTask(
            instruction="Send the price.",
            schedule="cron",
            cron="0 8 * * *",
            timezone="Asia/Tehran",
            destination="owner",
            summary="every day at 8",
        )
        [task], _ = build_tasks(
            [planned], bot, [], api.state.pricing.plan("free"), 0, datetime.now(UTC)
        )
        await replace_bot_tasks(session, bot, [task])
        await session.commit()
        return task.id


# --- initData ----------------------------------------------------------------------------


def test_init_data_roundtrip() -> None:
    user = validate_init_data(sign_init_data(ALICE, TOKEN), TOKEN)

    assert user.id == 1001 and user.language_code == "fa"


def test_init_data_with_wrong_token_is_rejected() -> None:
    with pytest.raises(InvalidInitData, match="bad signature"):
        validate_init_data(sign_init_data(ALICE, "999:other"), TOKEN)


def test_tampered_init_data_is_rejected() -> None:
    data = sign_init_data(ALICE, TOKEN).replace("1001", "2002")

    with pytest.raises(InvalidInitData, match="bad signature"):
        validate_init_data(data, TOKEN)


def test_old_init_data_is_rejected() -> None:
    old = int((datetime.now(UTC) - timedelta(days=2)).timestamp())

    with pytest.raises(InvalidInitData, match="expired"):
        validate_init_data(sign_init_data(ALICE, TOKEN, auth_date=old), TOKEN)


async def test_requests_without_init_data_are_rejected(api) -> None:
    assert (await api.get("/api/me")).status_code == 401
    bad = {"Authorization": "tma hash=abc&auth_date=1"}
    assert (await api.get("/api/me", headers=bad)).status_code == 401


# --- me ----------------------------------------------------------------------------------


async def test_me_creates_user_with_language_defaults(api) -> None:
    response = await api.get("/api/me", headers=auth(ALICE))

    assert response.status_code == 200
    me = response.json()
    assert me["language"] == "fa"
    assert me["timezone"] == "Asia/Tehran"
    assert me["messages_today"] == {"used": 0, "limit": 30}


async def test_update_me_validates_timezone(api) -> None:
    ok = await api.patch("/api/me", json={"timezone": "Europe/Berlin"}, headers=auth(ALICE))
    bad = await api.patch("/api/me", json={"timezone": "Mars/Base"}, headers=auth(ALICE))

    assert ok.json()["timezone"] == "Europe/Berlin"
    assert bad.status_code == 422


# --- bots --------------------------------------------------------------------------------


async def test_users_only_see_their_own_bots(api) -> None:
    alice_bot = await make_bot(api, ALICE, 11)
    await make_bot(api, BOB, 22)

    bots = (await api.get("/api/bots", headers=auth(ALICE))).json()

    assert [b["id"] for b in bots] == [alice_bot]
    assert (await api.get(f"/api/bots/{alice_bot}", headers=auth(BOB))).status_code == 404
    patch = await api.patch(f"/api/bots/{alice_bot}", json={"tools": []}, headers=auth(BOB))
    assert patch.status_code == 404


async def test_update_bot(api) -> None:
    bot_id = await make_bot(api, ALICE, 11)

    response = await api.patch(
        f"/api/bots/{bot_id}",
        json={
            "display_name": "Dollar",
            "description": "Daily dollar price",
            "system_prompt": "Be brief.",
            "tools": ["read_url", "memory"],
            "daily_credit_cap": 500,
        },
        headers=auth(ALICE),
    )

    bot = response.json()
    assert response.status_code == 200
    assert bot["display_name"] == "Dollar"
    assert bot["description"] == "Daily dollar price"
    assert bot["system_prompt"] == "Be brief."
    assert bot["tools"] == ["read_url", "memory"]
    assert bot["daily_credit_cap"] == 500

    cleared = await api.patch(
        f"/api/bots/{bot_id}", json={"clear_daily_credit_cap": True}, headers=auth(ALICE)
    )
    assert cleared.json()["daily_credit_cap"] is None


async def test_unknown_tool_is_rejected(api) -> None:
    bot_id = await make_bot(api, ALICE, 11)

    response = await api.patch(
        f"/api/bots/{bot_id}", json={"tools": ["shell"]}, headers=auth(ALICE)
    )

    assert response.status_code == 422


async def test_new_bot_from_panel_prepares_awaiting_draft(api) -> None:
    response = await api.post(
        "/api/bots/drafts",
        json={"name": "Dollar Watch", "system_prompt": "Report the dollar price."},
        headers=auth(ALICE),
    )

    body = response.json()
    assert body["create_link"].startswith("https://t.me/newbot/obotfactorybot/dollar_watch_")
    async with api.state.sessionmaker() as session:
        draft = await session.scalar(select(FactoryDraft).order_by(FactoryDraft.id.desc()))
    assert draft.state == DraftState.AWAITING_CREATION


# --- tasks -------------------------------------------------------------------------------


async def test_pause_resume_and_delete_task(api) -> None:
    bot_id = await make_bot(api, ALICE, 11)
    task_id = await make_task(api, bot_id)

    paused = await api.patch(f"/api/tasks/{task_id}", json={"paused": True}, headers=auth(ALICE))
    resumed = await api.patch(f"/api/tasks/{task_id}", json={"paused": False}, headers=auth(ALICE))
    forbidden = await api.delete(f"/api/tasks/{task_id}", headers=auth(BOB))
    deleted = await api.delete(f"/api/tasks/{task_id}", headers=auth(ALICE))

    assert paused.json()["status"] == TaskStatus.PAUSED
    assert resumed.json()["status"] == TaskStatus.ACTIVE
    assert resumed.json()["next_run_at"] is not None
    assert forbidden.status_code == 404
    assert deleted.status_code == 204
    detail = (await api.get(f"/api/bots/{bot_id}", headers=auth(ALICE))).json()
    assert detail["tasks"] == []


async def test_add_task_by_text(api) -> None:
    bot_id = await make_bot(api, ALICE, 11)
    api.state.models.model = TestModel(
        custom_output_args={
            "tasks": [
                {
                    "instruction": "Send the oil price.",
                    "schedule": "cron",
                    "cron": "0 9 * * *",
                    "timezone": "Asia/Tehran",
                    "destination": "owner",
                    "summary": "every day at 9",
                }
            ]
        }
    )

    response = await api.post(
        f"/api/bots/{bot_id}/tasks", json={"request": "oil price daily at 9"}, headers=auth(ALICE)
    )

    body = response.json()
    assert [t["summary"] for t in body["scheduled"]] == ["every day at 9"]
    assert body["scheduled"][0]["to_owner"] is True
    detail = (await api.get(f"/api/bots/{bot_id}", headers=auth(ALICE))).json()
    assert len(detail["tasks"]) == 1


async def test_usage_by_day(api) -> None:
    bot_id = await make_bot(api, ALICE, 11)
    async with api.state.sessionmaker() as session:
        bot = await session.get(Bot, bot_id, options=[joinedload(Bot.owner)])
        await record_llm_usage(session, api.state.pricing, bot.owner, bot, "test", 1000, 100)
        await session.commit()

    days = (await api.get(f"/api/bots/{bot_id}/usage", headers=auth(ALICE))).json()

    assert len(days) == 1
    assert days[0]["messages"] == 1 and days[0]["credits"] >= 1


def test_task_model_status_values_match_schema() -> None:
    assert {s.value for s in TaskStatus} >= {"active", "paused"}
    assert Task.__tablename__ == "tasks"
