from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx2
import pytest
from cryptography.exceptions import InvalidTag
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.models.test import TestModel
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from obot.billing.plans import Pricing
from obot.billing.usage import QuotaReason, check_llm_quota, count_messages, record_llm_usage
from obot.bots.byok import (
    delete_key,
    key_hint,
    read_key,
    reply_model,
    save_key,
    wallet,
    wallet_key,
)
from obot.bots.service import ManagedBotInfo, get_or_create_user, register_managed_bot
from obot.bots.users import bot_settings
from obot.core.byok import Byok, Failure, KeyCheckFailed, Provider, failure_of
from obot.db.models import Bot, BotVisibility, UsageEvent
from tests.test_api import ALICE, BOB, auth
from tests.test_api import make_bot as make_api_bot

PLANS_FILE = Path(__file__).parent.parent / "plans.yaml"
MODEL = "google/gemini-3.1-flash-lite"
KEY = "sk-test-0123456789abcd"


@pytest.fixture
def pricing() -> Pricing:
    return Pricing.load(PLANS_FILE)


async def make_bot(session, cipher, tg_bot_id: int = 1, creator: int = 1001) -> Bot:
    async def fetch_token(_: int) -> str:
        return "t"

    result = await register_managed_bot(
        session, cipher, fetch_token, creator, ManagedBotInfo(tg_bot_id, f"b{tg_bot_id}", "B"), 10
    )
    return await session.get(Bot, result.bot.id, options=[joinedload(Bot.owner)])


# --- failures ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "failure"),
    [
        (ModelHTTPError(401, "m"), Failure.AUTH),
        (ModelHTTPError(403, "m"), Failure.AUTH),
        (ModelHTTPError(402, "m"), Failure.QUOTA),
        (ModelHTTPError(429, "m"), Failure.QUOTA),
        (ModelHTTPError(404, "m"), Failure.MODEL),
        (ModelHTTPError(500, "m"), Failure.PROVIDER),
        (ModelAPIError("m", "connection reset"), Failure.PROVIDER),
        (ValueError("our bug"), None),
    ],
)
def test_failure_of(exc, failure) -> None:
    assert failure_of(exc) == failure


# --- the wallet and model choice -----------------------------------------------------------


async def use_own_key(session, cipher, user, bot, provider, model, api_key=KEY) -> None:
    """Put a key in `user`'s wallet and pick it with `model` for `bot`."""
    await save_key(session, cipher, user, provider, api_key)
    settings = await bot_settings(session, user, bot)
    settings.key_provider, settings.model = provider.value, model
    await session.flush()


async def test_wallet_keys_are_encrypted_and_bound_to_their_user(session, cipher) -> None:
    ali = await get_or_create_user(session, 5005)
    sara = await get_or_create_user(session, 6006)

    row = await save_key(session, cipher, ali, Provider.OPENAI, KEY)
    replaced = await save_key(session, cipher, ali, Provider.OPENAI, KEY[:-4] + "wxyz")

    assert replaced.id == row.id and len(await wallet(session, ali)) == 1
    assert KEY.encode() not in row.key_encrypted
    assert key_hint(cipher, row) == "…wxyz"
    stolen = await save_key(session, cipher, sara, Provider.OPENAI, "sk-sara-0000")
    stolen.key_encrypted = row.key_encrypted
    with pytest.raises(InvalidTag):
        read_key(cipher, stolen)

    await delete_key(session, ali, Provider.OPENAI)
    assert await wallet_key(session, ali, "openai") is None


def fake_models(tier_model):
    built = []

    def build(provider, name, api_key):
        built.append((provider, name, api_key))
        return "byok-model"

    return SimpleNamespace(
        for_tier=lambda tier: tier_model, byok=SimpleNamespace(model=build)
    ), built


async def test_each_person_answers_with_their_own_key_and_model(session, cipher, pricing) -> None:
    bot = await make_bot(session, cipher)
    ali = await get_or_create_user(session, 5005)
    tier_model = TestModel()
    models, built = fake_models(tier_model)

    assert (await reply_model(session, models, cipher, pricing, bot, ali)).model is tier_model

    await use_own_key(session, cipher, ali, bot, Provider.ANTHROPIC, "claude-haiku-4-5")
    chosen = await reply_model(session, models, cipher, pricing, bot, ali)
    owner_chosen = await reply_model(session, models, cipher, pricing, bot, bot.owner)

    assert chosen.byok is True and chosen.model == "byok-model"
    assert built == [(Provider.ANTHROPIC, "claude-haiku-4-5", KEY)]
    assert owner_chosen.byok is False  # Ali's key is never used for anyone else

    await delete_key(session, ali, Provider.ANTHROPIC)  # the bot falls back to Obot's
    assert (await reply_model(session, models, cipher, pricing, bot, ali)).byok is False


async def test_recommended_tier_beyond_the_plan_is_downgraded(session, cipher, pricing) -> None:
    bot = await make_bot(session, cipher)
    bot.model_tier = "strong"
    ali = await get_or_create_user(session, 5005)
    tiers = []
    models = SimpleNamespace(for_tier=lambda tier: tiers.append(tier) or TestModel(), byok=None)

    free = await reply_model(session, models, cipher, pricing, bot, ali)
    ali.plan = "paid"
    paid = await reply_model(session, models, cipher, pricing, bot, ali)

    assert tiers == ["cheap", "strong"]
    assert free.downgraded and not paid.downgraded


def test_models_are_built_for_each_provider() -> None:
    byok = Byok(256)

    openrouter = byok.model(Provider.OPENROUTER, "meta-llama/llama-4", KEY)
    openai = byok.model(Provider.OPENAI, "gpt-5-mini", KEY)
    anthropic = byok.model(Provider.ANTHROPIC, "claude-haiku-4-5", KEY)

    assert isinstance(openrouter, OpenRouterModel)
    assert isinstance(openai, OpenAIChatModel) and openai.system == "openai"
    assert isinstance(anthropic, AnthropicModel)
    assert anthropic.model_name == "claude-haiku-4-5"
    assert anthropic.settings["max_tokens"] == 256


# --- key check ------------------------------------------------------------------------------


def byok_with(handler) -> tuple[Byok, list[httpx2.Request]]:
    seen: list[httpx2.Request] = []

    def record(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return handler(request)

    return Byok(256, http=httpx2.AsyncClient(transport=httpx2.MockTransport(record))), seen


async def test_openrouter_check_validates_key_and_model() -> None:
    def handler(request):
        if request.url.path.endswith("/key"):
            ok = request.headers["authorization"] == f"Bearer {KEY}"
            return httpx2.Response(200 if ok else 401, json={})
        return httpx2.Response(200, json={"data": [{"id": "meta-llama/llama-4"}]})

    byok, seen = byok_with(handler)

    await byok.check(Provider.OPENROUTER, "meta-llama/llama-4", KEY)
    with pytest.raises(KeyCheckFailed) as unknown:
        await byok.check(Provider.OPENROUTER, "nope/model", KEY)
    with pytest.raises(KeyCheckFailed) as bad_key:
        await byok.check(Provider.OPENROUTER, "meta-llama/llama-4", "sk-wrong-key")

    assert unknown.value.failure == Failure.MODEL
    assert bad_key.value.failure == Failure.AUTH
    assert str(seen[0].url) == "https://openrouter.ai/api/v1/key"


async def test_openai_and_anthropic_checks_ask_for_the_model() -> None:
    def handler(request):
        return httpx2.Response(404 if "missing" in request.url.path else 200, json={})

    byok, seen = byok_with(handler)

    await byok.check(Provider.OPENAI, "ft:gpt-4o:org::abc", KEY)
    await byok.check(Provider.ANTHROPIC, "claude-haiku-4-5", KEY)
    with pytest.raises(KeyCheckFailed) as missing:
        await byok.check(Provider.ANTHROPIC, "missing-model", KEY)

    assert str(seen[0].url) == "https://api.openai.com/v1/models/ft%3Agpt-4o%3Aorg%3A%3Aabc"
    assert seen[0].headers["authorization"] == f"Bearer {KEY}"
    assert str(seen[1].url) == "https://api.anthropic.com/v1/models/claude-haiku-4-5"
    assert seen[1].headers["x-api-key"] == KEY
    assert "authorization" not in seen[1].headers
    assert missing.value.failure == Failure.MODEL


async def test_check_reports_unreachable_provider() -> None:
    def handler(request):
        raise httpx2.ConnectError("no route")

    byok, _ = byok_with(handler)

    with pytest.raises(KeyCheckFailed) as failed:
        await byok.check(Provider.OPENAI, "gpt-5-mini", KEY)
    assert failed.value.failure == Failure.PROVIDER


# --- billing --------------------------------------------------------------------------------


async def test_byok_replies_are_free_and_do_not_count_as_messages(session, cipher, pricing) -> None:
    bot = await make_bot(session, cipher)
    owner = bot.owner
    owner.plan, owner.credit_balance = "paid", 100
    now = datetime.now(UTC)

    event = await record_llm_usage(
        session, pricing, owner, bot, "some/unpriced-model", 5000, 500, byok=True
    )

    assert (event.byok, event.cost_usd, event.credits) == (True, 0, 0)
    assert owner.credit_balance == 100
    assert await count_messages(session, owner, now.replace(year=2000)) == 0


async def test_byok_ignores_message_limit_and_balance(session, cipher, pricing) -> None:
    bot = await make_bot(session, cipher)
    owner = bot.owner
    now = datetime.now(UTC)
    for _ in range(pricing.plan("free").daily_llm_messages):
        await record_llm_usage(session, pricing, owner, bot, MODEL, 10, 10)

    assert (await check_llm_quota(session, pricing, owner, now)).reason == (
        QuotaReason.DAILY_MESSAGES
    )
    assert (await check_llm_quota(session, pricing, owner, now, byok=True)).allowed

    owner.plan, owner.credit_balance = "paid", 0
    assert (await check_llm_quota(session, pricing, owner, now)).reason == (QuotaReason.NO_CREDITS)
    assert (await check_llm_quota(session, pricing, owner, now, byok=True)).allowed


# --- API ------------------------------------------------------------------------------------


async def test_wallet_api_never_returns_keys(api) -> None:
    put = await api.put("/api/keys/openai", json={"api_key": f"  {KEY} "}, headers=auth(BOB))
    listed = await api.get("/api/keys", headers=auth(BOB))
    others = await api.get("/api/keys", headers=auth(ALICE))
    removed = await api.delete("/api/keys/openai", headers=auth(BOB))

    assert put.json() == [{"provider": "openai", "key_hint": "…abcd"}] == listed.json()
    assert KEY not in put.text and others.json() == [] and removed.json() == []


async def public_api_bot(api) -> int:
    bot_id = await make_api_bot(api, ALICE, 50)
    async with api.state.sessionmaker() as session:
        bot = await session.get(Bot, bot_id)
        bot.visibility, bot.recommended_model = BotVisibility.PUBLIC, "claude-sonnet-5.5"
        await session.commit()
    return bot_id


async def test_a_user_picks_their_key_and_model_for_a_bot(api) -> None:
    bot_id = await public_api_bot(api)
    await api.put("/api/keys/anthropic", json={"api_key": KEY}, headers=auth(BOB))
    url = f"/api/bots/{bot_id}/mine"

    before = (await api.get(url, headers=auth(BOB))).json()
    body = {"key_provider": "anthropic", "model": "claude-sonnet-5.5"}
    saved = await api.put(url, json=body, headers=auth(BOB))

    assert before["recommended_model"] == "claude-sonnet-5.5" and before["wallet"] == ["anthropic"]
    assert saved.status_code == 200, saved.text
    assert (saved.json()["key_provider"], saved.json()["model"]) == (
        "anthropic",
        "claude-sonnet-5.5",
    )
    assert not saved.json()["is_owner"]
    assert api.state.models.byok.checked == [("anthropic", "claude-sonnet-5.5", KEY)]
    alice_view = (await api.get(url, headers=auth(ALICE))).json()
    assert alice_view["key_provider"] is None  # settings are each person's own


async def test_failed_check_is_reported_and_not_saved(api) -> None:
    bot_id = await public_api_bot(api)
    await api.put("/api/keys/openai", json={"api_key": KEY}, headers=auth(BOB))
    api.state.models.byok.failure = Failure.AUTH
    url = f"/api/bots/{bot_id}/mine"

    response = await api.put(
        url, json={"key_provider": "openai", "model": "gpt-5-mini"}, headers=auth(BOB)
    )
    missing = await api.put(
        url, json={"key_provider": "openrouter", "model": "a/b"}, headers=auth(BOB)
    )

    assert (response.status_code, response.json()["detail"]) == (422, "auth")
    assert missing.status_code == 422
    assert (await api.get(url, headers=auth(BOB))).json()["key_provider"] is None


async def test_private_bots_settings_are_not_reachable_by_strangers(api) -> None:
    bot_id = await make_api_bot(api, ALICE, 51)

    assert (await api.get(f"/api/bots/{bot_id}/mine", headers=auth(BOB))).status_code == 404
    assert (await api.get(f"/api/bots/{bot_id}/mine", headers=auth(ALICE))).json()["is_owner"]


@pytest.mark.parametrize("model", ["", "a b", "x" * 200, "../etc"])
async def test_invalid_model_names_are_rejected(api, model) -> None:
    bot_id = await public_api_bot(api)
    await api.put("/api/keys/openai", json={"api_key": KEY}, headers=auth(BOB))
    body = {"key_provider": "openai", "model": model}

    response = await api.put(f"/api/bots/{bot_id}/mine", json=body, headers=auth(BOB))

    assert response.status_code == 422
    assert api.state.models.byok.checked == []


async def test_me_does_not_count_byok_replies(api, pricing) -> None:
    bot_id = await make_api_bot(api, ALICE, 50)
    async with api.state.sessionmaker() as session:
        bot = await session.get(Bot, bot_id, options=[joinedload(Bot.owner)])
        await record_llm_usage(session, pricing, bot.owner, bot, MODEL, 10, 10)
        await record_llm_usage(session, pricing, bot.owner, bot, "x/y", 10, 10, byok=True)
        await session.commit()
        assert len((await session.scalars(select(UsageEvent))).all()) == 2

    me = await api.get("/api/me", headers=auth(ALICE))

    assert me.json()["messages_today"]["used"] == 1
