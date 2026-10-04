from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from obot.api.auth import InvalidInitData, sign_init_data, validate_init_data
from obot.billing.usage import record_llm_usage
from obot.bots.service import ManagedBotInfo, get_or_create_user, register_managed_bot
from obot.db.models import Bot, DraftState, FactoryDraft, Task, TaskStatus
from obot.tasks.planner import PlannedTask
from obot.tasks.service import build_tasks, confirm_draft
from tests.conftest import API_FACTORY_TOKEN as TOKEN

ALICE = {"id": 1001, "first_name": "Alice", "language_code": "fa"}
BOB = {"id": 2002, "first_name": "Bob", "language_code": "en"}
PLANS_FILE = Path(__file__).parent.parent / "plans.yaml"


def auth(user: dict) -> dict[str, str]:
    return {"Authorization": f"tma {sign_init_data(user, TOKEN)}"}


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


async def make_task(api, bot_id: int, tg_user: dict = ALICE) -> int:
    """A confirmed task of `tg_user` on the bot."""
    async with api.state.sessionmaker() as session:
        bot = await session.get(Bot, bot_id, options=[joinedload(Bot.owner)])
        user = await get_or_create_user(session, tg_user["id"])
        plan = api.state.pricing.plan("free")
        planned = PlannedTask(
            instruction="Send the price.",
            schedule="cron",
            cron="0 8 * * *",
            timezone="Asia/Tehran",
            destination="me",
            summary="every day at 8",
        )
        [task], _ = build_tasks([planned], bot, user, [], plan, 0, datetime.now(UTC))
        session.add(task)
        await session.flush()
        await confirm_draft(session, task, user, plan, datetime.now(UTC))
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
            "recommended_model": "claude-sonnet-5.5",
        },
        headers=auth(ALICE),
    )

    bot = response.json()
    assert response.status_code == 200
    assert bot["display_name"] == "Dollar"
    assert bot["description"] == "Daily dollar price"
    assert bot["system_prompt"] == "Be brief."
    assert bot["tools"] == ["read_url", "memory"]
    assert bot["recommended_model"] == "claude-sonnet-5.5"

    cleared = await api.patch(
        f"/api/bots/{bot_id}", json={"recommended_model": " "}, headers=auth(ALICE)
    )
    assert cleared.json()["recommended_model"] is None


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
    assert (await api.get("/api/tasks", headers=auth(ALICE))).json() == []


async def test_my_tasks_are_mine_on_any_bot(api) -> None:
    bot_id = await make_bot(api, ALICE, 11)
    alice_task = await make_task(api, bot_id)
    bob_task = await make_task(api, bot_id, BOB)  # Bob uses Alice's bot

    alice = (await api.get("/api/tasks", headers=auth(ALICE))).json()
    bob = (await api.get("/api/tasks", headers=auth(BOB))).json()
    owner_pausing_bobs = await api.patch(
        f"/api/tasks/{bob_task}", json={"paused": True}, headers=auth(ALICE)
    )

    assert [t["id"] for t in alice] == [alice_task]
    assert [(t["id"], t["bot_username"], t["to_me"]) for t in bob] == [
        (bob_task, "bot11_bot", True)
    ]
    assert owner_pausing_bobs.status_code == 404


async def test_usage_by_day(api) -> None:
    bot_id = await make_bot(api, ALICE, 11)
    async with api.state.sessionmaker() as session:
        bot = await session.get(Bot, bot_id, options=[joinedload(Bot.owner)])
        await record_llm_usage(session, api.state.pricing, bot.owner, bot, "test", 1000, 100)
        await session.commit()

    days = (await api.get(f"/api/bots/{bot_id}/usage", headers=auth(ALICE))).json()

    assert len(days) == 7
    assert days[-1]["messages"] == 1 and days[-1]["credits"] >= 1
    assert all(d["messages"] == 0 for d in days[:-1])


def test_task_model_status_values_match_schema() -> None:
    assert {s.value for s in TaskStatus} >= {"active", "paused"}
    assert Task.__tablename__ == "tasks"
