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
from obot.bots.byok import clear_key, key_hint, read_key, reply_model, set_key, uses_byok
from obot.bots.service import ManagedBotInfo, get_or_create_user, register_managed_bot
from obot.bots.users import bot_settings
from obot.core.byok import Byok, Failure, KeyCheckFailed, Provider, failure_of
from obot.db.models import Bot, UsageEvent
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


# --- storage and model choice ---------------------------------------------------------------


async def test_key_is_encrypted_and_bound_to_the_bot(session, cipher) -> None:
    bot = await make_bot(session, cipher, 1)
    other = await make_bot(session, cipher, 2)

    set_key(cipher, bot, Provider.OPENAI, "gpt-5-mini", KEY)

    assert uses_byok(bot)
    assert KEY.encode() not in bot.byok_key_encrypted
    assert read_key(cipher, bot) == KEY
    assert key_hint(cipher, bot) == "…abcd"
    other.byok_key_encrypted = bot.byok_key_encrypted
    with pytest.raises(InvalidTag):
        read_key(cipher, other)

    clear_key(bot)
    assert not uses_byok(bot)
    assert (bot.byok_provider, bot.byok_model) == (None, None)


async def test_reply_model_prefers_the_bots_own_key(session, cipher, pricing) -> None:
    bot = await make_bot(session, cipher)
    tier_model = TestModel()
    built = []

    def build(provider, name, api_key):
        built.append((provider, name, api_key))
        return "byok-model"

    models = SimpleNamespace(for_tier=lambda tier: tier_model, byok=SimpleNamespace(model=build))

    owner = bot.owner
    assert reply_model(models, cipher, pricing, bot, owner).byok is False
    assert reply_model(models, cipher, pricing, bot, owner).model is tier_model

    set_key(cipher, bot, Provider.ANTHROPIC, "claude-haiku-4-5", KEY)
    chosen = reply_model(models, cipher, pricing, bot, owner)

    assert chosen.byok is True and chosen.model == "byok-model"
    assert built == [(Provider.ANTHROPIC, "claude-haiku-4-5", KEY)]


async def test_owner_key_is_never_used_for_others(session, cipher, pricing) -> None:
    bot = await make_bot(session, cipher)
    set_key(cipher, bot, Provider.ANTHROPIC, "claude-haiku-4-5", KEY)
    ali = await get_or_create_user(session, 5005)
    tier_model = TestModel()
    models = SimpleNamespace(for_tier=lambda tier: tier_model, byok=None)

    chosen = reply_model(models, cipher, pricing, bot, ali)

    assert chosen.byok is False and chosen.model is tier_model


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


async def test_byok_ignores_message_limit_and_balance_but_not_the_bot_cap(
    session, cipher, pricing
) -> None:
    bot = await make_bot(session, cipher)
    owner = bot.owner
    now = datetime.now(UTC)
    for _ in range(pricing.plan("free").daily_llm_messages):
        await record_llm_usage(session, pricing, owner, bot, MODEL, 10, 10)

    assert (await check_llm_quota(session, pricing, owner, bot, now)).reason == (
        QuotaReason.DAILY_MESSAGES
    )
    assert (await check_llm_quota(session, pricing, owner, bot, now, byok=True)).allowed

    owner.plan, owner.credit_balance = "paid", 0
    assert (await check_llm_quota(session, pricing, owner, bot, now)).reason == (
        QuotaReason.NO_CREDITS
    )
    assert (await check_llm_quota(session, pricing, owner, bot, now, byok=True)).allowed

    (await bot_settings(session, owner, bot)).daily_credit_cap = 1
    decision = await check_llm_quota(session, pricing, owner, bot, now, byok=True)
    assert decision.reason == QuotaReason.BOT_DAILY_CAP


# --- API ------------------------------------------------------------------------------------


async def test_set_byok_checks_and_stores_the_key(api) -> None:
    bot_id = await make_api_bot(api, ALICE, 50)
    body = {"provider": "openai", "model": "gpt-5-mini", "api_key": f"  {KEY} "}

    response = await api.put(f"/api/bots/{bot_id}/byok", json=body, headers=auth(ALICE))

    assert response.status_code == 200, response.text
    assert response.json()["byok"] == {
        "provider": "openai",
        "model": "gpt-5-mini",
        "key_hint": "…abcd",
    }
    assert KEY not in response.text
    assert api.state.models.byok.checked == [("openai", "gpt-5-mini", KEY)]
    async with api.state.sessionmaker() as session:
        bot = await session.get(Bot, bot_id)
        assert read_key(api.state.cipher, bot) == KEY


async def test_failed_check_is_reported_and_not_stored(api) -> None:
    bot_id = await make_api_bot(api, ALICE, 50)
    api.state.models.byok.failure = Failure.AUTH
    body = {"provider": "anthropic", "model": "claude-haiku-4-5", "api_key": KEY}

    response = await api.put(f"/api/bots/{bot_id}/byok", json=body, headers=auth(ALICE))

    assert response.status_code == 422
    assert response.json()["detail"] == "auth"
    detail = await api.get(f"/api/bots/{bot_id}", headers=auth(ALICE))
    assert detail.json()["byok"] is None


async def test_switching_model_keeps_the_key_of_the_same_provider(api) -> None:
    bot_id = await make_api_bot(api, ALICE, 50)
    url = f"/api/bots/{bot_id}/byok"
    first = {"provider": "openrouter", "model": "a/one", "api_key": KEY}
    await api.put(url, json=first, headers=auth(ALICE))

    switched = await api.put(
        url, json={"provider": "openrouter", "model": "b/two"}, headers=auth(ALICE)
    )
    other_provider = await api.put(
        url, json={"provider": "openai", "model": "gpt-5-mini"}, headers=auth(ALICE)
    )

    assert switched.json()["byok"]["model"] == "b/two"
    assert api.state.models.byok.checked[-1] == ("openrouter", "b/two", KEY)
    assert other_provider.status_code == 422


async def test_delete_byok_and_ownership(api) -> None:
    bot_id = await make_api_bot(api, ALICE, 50)
    url = f"/api/bots/{bot_id}/byok"
    body = {"provider": "openai", "model": "gpt-5-mini", "api_key": KEY}

    assert (await api.put(url, json=body, headers=auth(BOB))).status_code == 404
    assert (await api.delete(url, headers=auth(BOB))).status_code == 404
    await api.put(url, json=body, headers=auth(ALICE))
    deleted = await api.delete(url, headers=auth(ALICE))

    assert deleted.json()["byok"] is None
    async with api.state.sessionmaker() as session:
        bot = await session.get(Bot, bot_id)
        assert bot.byok_key_encrypted is None


@pytest.mark.parametrize("model", ["", "a b", "x" * 200, "../etc"])
async def test_invalid_model_names_are_rejected(api, model) -> None:
    bot_id = await make_api_bot(api, ALICE, 50)
    body = {"provider": "openai", "model": model, "api_key": KEY}

    response = await api.put(f"/api/bots/{bot_id}/byok", json=body, headers=auth(ALICE))

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
