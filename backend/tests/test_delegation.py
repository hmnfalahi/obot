from pathlib import Path
from types import SimpleNamespace

import httpx
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from obot.billing.plans import ModelPrice, Pricing
from obot.bots.service import ManagedBotInfo, register_managed_bot
from obot.core.agent import BotProfile, generate_reply
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


def runtime_with(session, model) -> SimpleNamespace:
    pricing = Pricing.load(PLANS_FILE)
    function_model = FunctionModel(model.respond)
    pricing.models[function_model.model_name] = ModelPrice(input_per_mtok=1, output_per_mtok=1)
    return SimpleNamespace(
        sessionmaker=async_sessionmaker(session.bind, expire_on_commit=False),
        pricing=pricing,
        models=SimpleNamespace(for_tier=lambda tier: function_model),
        http=httpx.AsyncClient(),
        tavily_api_key=None,
    )


async def test_delegate_answers_with_callee_and_bills_it(session, cipher) -> None:
    caller = await make_bot(session, cipher, 1, "asst_bot", tools=["ask_bots"])
    callee = await make_bot(session, cipher, 2, "fr_bot", tools=["web_search", "ask_bots"])
    await session.commit()
    model = AnsweringModel("Bonjour")
    delegate = Delegator(runtime_with(session, model), caller)

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
    delegate = Delegator(runtime_with(session, AnsweringModel()), caller)

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
