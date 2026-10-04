from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from obot.billing.plans import ModelPrice, Pricing
from obot.bots.byok import set_key
from obot.bots.service import ManagedBotInfo, register_managed_bot
from obot.config import get_settings
from obot.core.agent import BotProfile, generate_reply
from obot.core.byok import Provider
from obot.core.tools import ToolServices, build_tools
from obot.db.models import UsageEvent
from obot.telegram.delegation import Delegator, delegation_note, owner_peers

PLANS_FILE = Path(__file__).parent.parent / "plans.yaml"


async def make_bot(session, cipher, tg_bot_id: int, username: str, owner: int = 1001, tools=()):
    async def fetch_token(_: int) -> str:
        return f"{tg_bot_id}:t"

    result = await register_managed_bot(
        session, cipher, fetch_token, owner, ManagedBotInfo(tg_bot_id, username, username), 10
    )
    result.bot.tools = list(tools)
    result.bot.persona = {"description": f"{username} description"}
    await session.flush()
    return result.bot


class AnsweringModel:
    """Records what the callee saw and which tools it was given."""

    def __init__(self, answer: str = "Bonjour") -> None:
        self.seen_tools: list[list[str]] = []
        self.answer = answer

    def respond(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.seen_tools.append([t.name for t in info.function_tools])
        return ModelResponse(parts=[TextPart(self.answer)])


def runtime_with(session, model, cipher=None, **extra) -> SimpleNamespace:
    pricing = Pricing.load(PLANS_FILE)
    function_model = FunctionModel(model.respond)
    pricing.models[function_model.model_name] = ModelPrice(input_per_mtok=1, output_per_mtok=1)
    return SimpleNamespace(
        sessionmaker=async_sessionmaker(session.bind, expire_on_commit=False),
        cipher=cipher,
        pricing=pricing,
        models=SimpleNamespace(for_tier=lambda tier: function_model),
        http=httpx.AsyncClient(),
        tavily_api_key=None,
        **extra,
    )


async def test_delegate_answers_with_callee_and_bills_it(session, cipher) -> None:
    caller = await make_bot(session, cipher, 1, "asst_bot", tools=["ask_bots"])
    callee = await make_bot(session, cipher, 2, "fr_bot", tools=["web_search", "ask_bots"])
    await session.commit()
    model = AnsweringModel("Bonjour")
    delegate = Delegator(runtime_with(session, model, cipher), caller)

    answer = await delegate("FR_bot", "Translate hello to French")

    assert answer == "Bonjour"
    # Depth 1: the callee keeps its tools but cannot delegate again.
    assert model.seen_tools == [["web_search"]]
    billed = list(await session.scalars(select(UsageEvent.bot_id)))
    assert billed == [callee.id]


async def test_cannot_ask_bots_of_other_owners_or_itself(session, cipher) -> None:
    caller = await make_bot(session, cipher, 1, "asst_bot", tools=["ask_bots"])
    await make_bot(session, cipher, 3, "stranger_bot", owner=2002)
    await session.commit()
    delegate = Delegator(runtime_with(session, AnsweringModel(), cipher), caller)

    assert (await delegate("stranger_bot", "hi")).startswith("ERROR")
    assert (await delegate("asst_bot", "hi")).startswith("ERROR")
    assert (await delegate("missing_bot", "hi")).startswith("ERROR")


async def test_peers_are_listed_for_the_caller(session, cipher) -> None:
    caller = await make_bot(session, cipher, 1, "asst_bot", tools=["ask_bots"])
    await make_bot(session, cipher, 2, "fr_bot")
    await make_bot(session, cipher, 3, "stranger_bot", owner=2002)

    note = delegation_note(await owner_peers(session, caller))

    assert "@fr_bot" in note and "fr_bot description" in note
    assert "stranger_bot" not in note
    assert delegation_note([]) is None


async def test_ask_bot_tool_calls_the_delegate() -> None:
    asked = []

    async def delegate(username: str, question: str) -> str:
        asked.append((username, question))
        return "Bonjour"

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        returns = [p.content for m in messages for p in m.parts if p.part_kind == "tool-return"]
        if returns:
            return ModelResponse(parts=[TextPart(f"They said: {returns[-1]}")])
        return ModelResponse(
            parts=[ToolCallPart("ask_bot", {"username": "@fr_bot", "question": "hello in French?"})]
        )

    services = ToolServices(http=httpx.AsyncClient(), meter=None, delegate=delegate)
    reply = await generate_reply(
        FunctionModel(respond),
        BotProfile("A"),
        [],
        "go",
        tools=build_tools(["ask_bots"]),
        services=services,
    )

    assert asked == [("fr_bot", "hello in French?")]
    assert reply.text == "They said: Bonjour"


class ByokModels:
    """Platform tier model plus models built for owners' keys."""

    def __init__(self, platform, own) -> None:
        self.platform, self.own = platform, own
        self.built: list[tuple] = []
        self.byok = self

    def for_tier(self, tier: str):
        return self.platform

    def model(self, provider, name, api_key):
        self.built.append((provider, name, api_key))
        return self.own


class FailingModel:
    def respond(self, messages, info):
        raise ModelHTTPError(401, "gpt-5-mini", {"error": "invalid key"})


@pytest.fixture
async def redis():
    client = Redis.from_url(get_settings().redis_url, db=15)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


def byok_runtime(session, cipher, redis, own_model):
    sent = []

    async def send_message(chat_id, text):
        sent.append((chat_id, text))

    runtime = runtime_with(
        session,
        AnsweringModel("platform"),
        cipher,
        redis=redis,
        factory_bot=SimpleNamespace(send_message=send_message),
    )
    runtime.models = ByokModels(runtime.models.for_tier("cheap"), FunctionModel(own_model.respond))
    return runtime, sent


async def test_callee_answers_with_its_own_key_for_free(session, cipher, redis) -> None:
    caller = await make_bot(session, cipher, 1, "asst_bot", tools=["ask_bots"])
    callee = await make_bot(session, cipher, 2, "fr_bot")
    set_key(cipher, callee, Provider.OPENAI, "gpt-5-mini", "sk-own-key-1234")
    await session.commit()
    runtime, sent = byok_runtime(session, cipher, redis, AnsweringModel("Bonjour (own key)"))

    answer = await Delegator(runtime, caller)("fr_bot", "hello in French?")

    assert answer == "Bonjour (own key)"
    assert runtime.models.built == [(Provider.OPENAI, "gpt-5-mini", "sk-own-key-1234")]
    event = await session.scalar(select(UsageEvent))
    assert (event.byok, event.credits) == (True, 0)
    assert sent == []


async def test_failing_own_key_tells_the_owner_once(session, cipher, redis) -> None:
    caller = await make_bot(session, cipher, 1, "asst_bot", tools=["ask_bots"])
    callee = await make_bot(session, cipher, 2, "fr_bot")
    set_key(cipher, callee, Provider.OPENAI, "gpt-5-mini", "sk-own-key-1234")
    await session.commit()
    runtime, sent = byok_runtime(session, cipher, redis, FailingModel())
    delegate = Delegator(runtime, caller)

    first = await delegate("fr_bot", "hello?")
    await delegate("fr_bot", "hello again?")

    assert first.startswith("ERROR")
    assert len(sent) == 1
    owner_chat, text = sent[0]
    assert owner_chat == 1001
    assert "@fr_bot" in text and "OpenAI" in text and "invalid" in text
