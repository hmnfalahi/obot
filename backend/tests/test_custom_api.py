import json
from types import SimpleNamespace

import httpx
import pytest
from cryptography.exceptions import InvalidTag
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from obot.bots.apis import (
    bot_key_slots,
    key_values,
    load_endpoints,
    set_key_value,
    write_endpoint,
)
from obot.bots.service import ManagedBotInfo, get_or_create_user, register_managed_bot
from obot.core.agent import BotProfile, generate_reply
from obot.core.custom_api import (
    Endpoint,
    InvalidEndpoint,
    MissingKeys,
    endpoints_note,
    fill,
    key_slots,
    request,
    validate_endpoint,
)
from obot.core.fetch import FetchError
from obot.core.tools import ToolServices, build_tools
from obot.db.models import ApiEndpoint, ApiKeyValue, Bot
from tests.test_api import ALICE, BOB, auth
from tests.test_api import make_bot as make_api_bot
from tests.test_tools import FakeMeter

SECRET = "s3cret-token"
KEYS = {"shop_token": SECRET}
ORDERS = Endpoint(
    name="order_status",
    description="Status of a shop order by its id",
    method="GET",
    url="https://shop.example/api/orders/{order_id}?lang=fa",
    headers={"Authorization": "Bearer {shop_token}"},
)


def client(handler) -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return handler(req)

    return httpx.AsyncClient(transport=httpx.MockTransport(record)), seen


# --- definitions ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "method", "url", "headers"),
    [
        ("Bad Name", "GET", "https://a.example/x", {}),
        ("ok", "TRACE", "https://a.example/x", {}),
        ("ok", "GET", "ftp://a.example/x", {}),
        ("ok", "GET", "a.example/x", {}),
        ("ok", "GET", "https://{tenant}.example/x", {}),
        ("ok", "GET", "https://a.example/{bad-name}", {}),
        ("ok", "GET", "https://a.example/x", {"Host": "evil.example"}),
        ("ok", "GET", "https://a.example/x", {"X-Key": "a\r\nInjected: 1"}),
        ("ok", "GET", "https://a.example/x", {f"X-{i}": "v" for i in range(11)}),
        ("ok", "GET", "https://a.example/x", {"Authorization": "Bearer {Shop-Token}"}),
        ("ok", "GET", "https://a.example/x", {"Authorization": "Bearer {token"}),
    ],
)
def test_invalid_endpoints_are_rejected(name, method, url, headers) -> None:
    with pytest.raises(InvalidEndpoint):
        validate_endpoint(name, method, url, headers)


def test_valid_endpoint() -> None:
    validate_endpoint("order_status", "POST", ORDERS.url, {"Authorization": SECRET})


def test_fill_quotes_values_and_requires_all() -> None:
    assert fill(ORDERS.url, {"order_id": "12/../admin"}) == (
        "https://shop.example/api/orders/12%2F..%2Fadmin?lang=fa"
    )
    with pytest.raises(FetchError, match="order_id"):
        fill(ORDERS.url, {})


async def test_missing_keys_stop_the_call_before_any_request(public_dns) -> None:
    http, seen = client(lambda r: httpx.Response(200))

    with pytest.raises(MissingKeys) as missing:
        await request(http, ORDERS, {"order_id": 1}, {}, None, {})

    assert missing.value.names == ["shop_token"] and seen == []
    assert key_slots({"A": "{x} and {y}", "B": "{x}"}) == ["x", "y"]


def test_note_lists_endpoints_without_addresses_or_secrets() -> None:
    note = endpoints_note({"order_status": ORDERS})

    assert "order_status (GET; path_params: order_id)" in note
    assert "Status of a shop order" in note
    assert "shop.example" not in note and "s3cret" not in note
    assert endpoints_note({}) is None


# --- calls ------------------------------------------------------------------------------------


async def test_request_sends_secret_headers_query_and_filled_path(public_dns) -> None:
    http, seen = client(lambda r: httpx.Response(200, json={"status": "shipped"}))

    text = await request(http, ORDERS, {"order_id": 42}, {"verbose": 1}, {"ignored": True}, KEYS)

    assert text.startswith("HTTP 200") and '"status": "shipped"' in text
    sent = seen[0]
    assert sent.method == "GET"
    assert str(sent.url) == "https://shop.example/api/orders/42?lang=fa&verbose=1"
    assert sent.headers["authorization"] == f"Bearer {SECRET}"
    assert sent.content == b""  # GET carries no body


async def test_post_sends_json_body(public_dns) -> None:
    endpoint = Endpoint("create_ticket", "Open a ticket", "POST", "https://desk.example/tickets")
    http, seen = client(lambda r: httpx.Response(201, text="created"))

    text = await request(http, endpoint, {}, {}, {"title": "Broken", "tags": ["a"]})

    assert text == "HTTP 201\ncreated"
    assert json.loads(seen[0].content) == {"title": "Broken", "tags": ["a"]}


async def test_redirects_are_not_followed(public_dns) -> None:
    http, seen = client(
        lambda r: httpx.Response(302, headers={"location": "https://other.example/steal"})
    )

    with pytest.raises(FetchError, match="redirected"):
        await request(http, ORDERS, {"order_id": 1}, {}, None, KEYS)
    assert len(seen) == 1


async def test_private_hosts_are_blocked(public_dns) -> None:
    endpoint = Endpoint("internal", "x", "GET", "http://internal.test/admin")
    http, seen = client(lambda r: httpx.Response(200))

    with pytest.raises(FetchError, match="not a public address"):
        await request(http, endpoint, {}, {}, None)
    assert seen == []


async def test_error_status_is_reported(public_dns) -> None:
    http, _ = client(lambda r: httpx.Response(404, json={"error": "no such order"}))

    with pytest.raises(FetchError, match="(?s)HTTP 404.*no such order"):
        await request(http, ORDERS, {"order_id": 9}, {}, None, KEYS)


async def test_call_api_tool_is_metered_and_hides_secrets(public_dns) -> None:
    http, seen = client(lambda r: httpx.Response(200, json={"status": "shipped"}))
    meter = FakeMeter()

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        returns = [p.content for m in messages for p in m.parts if p.part_kind == "tool-return"]
        if returns:
            return ModelResponse(parts=[TextPart(f"Order: {returns[-1]}")])
        return ModelResponse(
            parts=[
                ToolCallPart("call_api", {"name": "order_status", "path_params": {"order_id": "7"}})
            ]
        )

    services = ToolServices(http=http, meter=meter, apis={"order_status": ORDERS}, api_keys=KEYS)
    reply = await generate_reply(
        FunctionModel(respond),
        BotProfile("Shop"),
        [],
        "where is order 7?",
        tools=build_tools([], apis=True),
        services=services,
    )

    assert "shipped" in reply.text and SECRET not in reply.text
    assert str(seen[0].url).startswith("https://shop.example/api/orders/7")
    assert meter.used == ["api"]


async def test_call_api_asks_for_missing_keys(public_dns) -> None:
    http, seen = client(lambda r: httpx.Response(200))
    asked = []

    async def ask(names: list[str]) -> str:
        asked.append(names)
        return "a message with buttons was sent"

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        returns = [p.content for m in messages for p in m.parts if p.part_kind == "tool-return"]
        if returns:
            return ModelResponse(parts=[TextPart(returns[-1])])
        return ModelResponse(
            parts=[
                ToolCallPart("call_api", {"name": "order_status", "path_params": {"order_id": "7"}})
            ]
        )

    services = ToolServices(
        http=http, meter=FakeMeter(), apis={"order_status": ORDERS}, ask_for_keys=ask
    )
    reply = await generate_reply(
        FunctionModel(respond),
        BotProfile("Shop"),
        [],
        "order 7?",
        tools=build_tools([], apis=True),
        services=services,
    )

    assert asked == [["shop_token"]] and seen == []
    assert reply.text == "a message with buttons was sent"


def test_call_api_tool_only_with_endpoints() -> None:
    assert [t.name for t in build_tools(["web_search"])] == ["web_search"]
    assert [t.name for t in build_tools(["web_search"], apis=True)] == ["web_search", "call_api"]


# --- storage ----------------------------------------------------------------------------------


async def make_bot(session, cipher, tg_bot_id: int) -> Bot:
    async def fetch_token(_: int) -> str:
        return "t"

    result = await register_managed_bot(
        session, cipher, fetch_token, 1001, ManagedBotInfo(tg_bot_id, f"b{tg_bot_id}", "B"), 10
    )
    return await session.get(Bot, result.bot.id, options=[joinedload(Bot.owner)])


async def test_each_person_keeps_their_own_keys_for_a_bot(session, cipher) -> None:
    bot = await make_bot(session, cipher, 1)
    row = ApiEndpoint(bot_id=bot.id)
    write_endpoint(
        row,
        name="orders",
        description="d",
        method="GET",
        url="https://a.example/o",
        headers={"Authorization": "Bearer {shop_token}"},
    )
    session.add(row)
    ali = await get_or_create_user(session, 5005)
    sara = await get_or_create_user(session, 6006)

    await set_key_value(session, cipher, ali, bot, "shop_token", "ali-token")

    assert await bot_key_slots(session, bot) == ["shop_token"]
    assert await key_values(session, cipher, ali, bot) == {"shop_token": "ali-token"}
    assert await key_values(session, cipher, sara, bot) == {}
    assert (await load_endpoints(session, bot))["orders"].headers == {
        "Authorization": "Bearer {shop_token}"
    }
    stored = await session.scalar(select(ApiKeyValue))
    assert b"ali-token" not in stored.value_encrypted
    stored.user_id = sara.id  # a value moved to someone else cannot be read
    with pytest.raises(InvalidTag):
        await key_values(session, cipher, sara, bot)


# --- panel API --------------------------------------------------------------------------------

ORDER_API = {
    "name": "order_status",
    "description": "Status of a shop order",
    "method": "GET",
    "url": "https://shop.example/api/orders/{order_id}",
    "headers": {"Authorization": "Bearer {shop_token}"},
}


async def test_add_update_and_delete_api(api, public_dns) -> None:
    bot_id = await make_api_bot(api, ALICE, 60)

    added = await api.post(f"/api/bots/{bot_id}/apis", json=ORDER_API, headers=auth(ALICE))

    assert added.status_code == 200, added.text
    [out] = added.json()["apis"]
    assert out["headers"] == {"Authorization": "Bearer {shop_token}"}
    assert out["key_slots"] == ["shop_token"]
    update = {**ORDER_API, "description": "Order status"}
    updated = await api.put(f"/api/apis/{out['id']}", json=update, headers=auth(ALICE))
    assert updated.json()["apis"][0]["description"] == "Order status"

    assert (await api.delete(f"/api/apis/{out['id']}", headers=auth(BOB))).status_code == 404
    deleted = await api.delete(f"/api/apis/{out['id']}", headers=auth(ALICE))
    assert deleted.json()["apis"] == []


async def test_api_rules(api, public_dns) -> None:
    bot_id = await make_api_bot(api, ALICE, 60)
    url = f"/api/bots/{bot_id}/apis"

    assert (await api.post(url, json=ORDER_API, headers=auth(BOB))).status_code == 404
    await api.post(url, json=ORDER_API, headers=auth(ALICE))
    duplicate = await api.post(url, json=ORDER_API, headers=auth(ALICE))
    private = await api.post(
        url, json={**ORDER_API, "name": "x", "url": "http://internal.test/a"}, headers=auth(ALICE)
    )
    bad_header = await api.post(
        url, json={**ORDER_API, "name": "y", "headers": {"Host": "h"}}, headers=auth(ALICE)
    )
    for name in ("b", "c"):
        await api.post(url, json={**ORDER_API, "name": name}, headers=auth(ALICE))
    over_limit = await api.post(url, json={**ORDER_API, "name": "d"}, headers=auth(ALICE))

    assert duplicate.status_code == 409
    assert private.status_code == 422 and "public" in private.json()["detail"]
    assert bad_header.status_code == 422
    assert over_limit.status_code == 403  # free plan: 3 per bot
    me = await api.get("/api/me", headers=auth(ALICE))
    assert me.json()["max_apis"] == 3


# --- toolkit ----------------------------------------------------------------------------------


async def test_toolkit_gives_call_api_to_bots_with_endpoints(session, cipher) -> None:
    from obot.telegram.toolkit import toolkit

    bot = await make_bot(session, cipher, 1)
    runtime = SimpleNamespace(
        cipher=cipher, http=None, pricing=None, tavily_api_key=None, sessionmaker=None
    )
    plain = await toolkit(session, runtime, bot, None, bot.owner)
    row = ApiEndpoint(bot_id=bot.id)
    write_endpoint(
        row,
        name="orders",
        description="d",
        method="GET",
        url="https://a.example/o",
        headers={"K": "{k}"},
    )
    session.add(row)
    await set_key_value(session, cipher, bot.owner, bot, "k", "owner-value")
    await session.flush()

    kit = await toolkit(session, runtime, bot, None, bot.owner)

    assert "call_api" not in [t.name for t in plain.tools] and plain.note is None
    assert "call_api" in [t.name for t in kit.tools]
    assert kit.services.apis["orders"].headers == {"K": "{k}"}
    assert kit.services.api_keys == {"k": "owner-value"}
    assert "orders (GET)" in kit.note


async def test_users_set_their_keys_in_the_panel(api, public_dns) -> None:
    bot_id = await make_api_bot(api, ALICE, 61)
    await api.post(f"/api/bots/{bot_id}/apis", json=ORDER_API, headers=auth(ALICE))
    url = f"/api/bots/{bot_id}/mine/keys/shop_token"

    before = (await api.get(f"/api/bots/{bot_id}/mine", headers=auth(ALICE))).json()
    saved = await api.put(url, json={"value": f" {SECRET} "}, headers=auth(ALICE))
    unknown = await api.put(
        f"/api/bots/{bot_id}/mine/keys/other", json={"value": "x"}, headers=auth(ALICE)
    )
    cleared = await api.delete(url, headers=auth(ALICE))

    assert before["api_keys"] == [{"name": "shop_token", "is_set": False}]
    assert saved.json()["api_keys"] == [{"name": "shop_token", "is_set": True}]
    assert SECRET not in saved.text and unknown.status_code == 404
    assert cleared.json()["api_keys"] == [{"name": "shop_token", "is_set": False}]
