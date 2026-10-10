from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from obot.billing.plans import ModelPrice, Pricing
from obot.bots.service import ManagedBotInfo, register_managed_bot
from obot.chat.history import ChatScope
from obot.core.byok import Provider
from obot.db.models import Bot, BotStatus, ScheduleKind, Task, UsageEvent
from obot.tasks.planner import KnownBot, PlannedStep, PlannedTask, build_prompt
from obot.tasks.service import build_tasks
from obot.telegram.task_runner import StepFailed, _error_text, _run_chain, step_prompt
from tests.test_byok import use_own_key

PLANS_FILE = Path(__file__).parent.parent / "plans.yaml"
NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
OWNER = 1001


async def make_bot(session, cipher, tg_bot_id: int, username: str, owner: int = OWNER) -> Bot:
    async def fetch_token(_: int) -> str:
        return f"{tg_bot_id}:t"

    result = await register_managed_bot(
        session, cipher, fetch_token, owner, ManagedBotInfo(tg_bot_id, username, username), 10
    )
    result.bot.owner_started = True
    await session.flush()
    return await session.get(Bot, result.bot.id, options=[joinedload(Bot.owner)])


def planned(steps: list[tuple[str, str]], **overrides) -> PlannedTask:
    values = {
        "instruction": "News digest",
        "schedule": "cron",
        "cron": "0 8 * * *",
        "timezone": "Asia/Tehran",
        "destination": "me",
        "summary": "every day at 8",
        "steps": [PlannedStep(instruction=i, bot=b) for i, b in steps],
    }
    return PlannedTask(**(values | overrides))


# --- planning ---------------------------------------------------------------------------------


async def test_steps_resolve_bots(session, cipher) -> None:
    bot = await make_bot(session, cipher, 1, "news_bot")
    plan = Pricing.load(PLANS_FILE).plan("free")
    peers = [KnownBot(77, "fr_bot", "translator")]
    item = planned([("Get the news", "self"), ("Summarize", "@news_bot"), ("Translate", "@FR_bot")])

    [task], rejected = build_tasks([item], bot, bot.owner, [], plan, 0, NOW, peers=peers)

    assert rejected == []
    assert task.steps == [
        {"instruction": "Get the news", "bot_id": None},
        {"instruction": "Summarize", "bot_id": None},
        {"instruction": "Translate", "bot_id": 77},
    ]


async def test_invalid_chains_are_rejected(session, cipher) -> None:
    bot = await make_bot(session, cipher, 1, "news_bot")
    plan = Pricing.load(PLANS_FILE).plan("free")
    too_long = planned([(f"step {i}", "self") for i in range(plan.max_chain_steps + 1)])
    stranger = planned([("Get", "self"), ("Translate", "@someone_else_bot")])
    single = planned([("Only one", "self")])

    tasks, rejected = build_tasks([too_long, stranger, single], bot, bot.owner, [], plan, 0, NOW)

    assert [r.reason for r in rejected] == [
        "a chain can have at most 5 steps",
        "@someone_else_bot is not one of your active bots",
    ]
    assert [t.steps for t in tasks] == [[]]  # one step is just a task


def test_planner_prompt_lists_other_bots() -> None:
    prompt = build_prompt(["x"], "UTC", [], NOW, [KnownBot(2, "fr_bot", "French translator")])

    assert "@fr_bot: French translator" in prompt


# --- prompts --------------------------------------------------------------------------------


def chain_task() -> Task:
    return Task(
        summary="daily digest",
        instruction="digest",
        steps=[
            {"instruction": "Get the news", "bot_id": None},
            {"instruction": "Summarize it", "bot_id": None},
            {"instruction": "Translate to English", "bot_id": 2},
        ],
    )


def test_step_prompts() -> None:
    task = chain_task()

    first = step_prompt(task, 0, None, "fa")
    middle = step_prompt(task, 1, "the news", "fa")
    last = step_prompt(task, 2, "the summary", "fa")

    assert "step 1 of 3" in first
    assert "passed to the next step" in first and "previous step" not in first
    assert "the news" in middle
    assert "the summary" in last and "Write the message to send now" in last
    assert "Unless this step names a language" in last and "'fa'" in last
    assert "'fa'" not in middle


def test_error_text() -> None:
    failed = StepFailed(1, "fr_bot", "byok.reason.auth")
    single = StepFailed(0, "news_bot", "byok.reason.quota", numbered=False)
    quota = StepFailed(2, "fr_bot", "quota: daily_messages")

    assert _error_text(failed, "en") == "step 2 (@fr_bot): the key is invalid or was revoked"
    assert _error_text(single, "en") == "the account is out of credit or rate-limited"
    assert _error_text(quota, None).startswith("step 3 (@fr_bot): You've used today's")
    assert _error_text(ValueError("boom"), None) == "ValueError: boom"


# --- running ----------------------------------------------------------------------------------


class Scripted:
    """A model answering as the bot named in its instructions, recording every prompt."""

    def __init__(self, fail_for: str | None = None) -> None:
        self.prompts: list[tuple[str, str]] = []
        self.tools: list[tuple[str, list[str]]] = []
        self.fail_for = fail_for

    def respond(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        name = info.instructions.split("Your name is ", 1)[1].split(".", 1)[0]
        prompt = messages[-1].parts[-1].content
        self.prompts.append((name, prompt))
        self.tools.append((name, [t.name for t in info.function_tools]))
        if name == self.fail_for:
            raise ModelHTTPError(401, "m", {"error": "bad key"})
        step = prompt.split("\n", 1)[1].split("\n", 1)[0]
        return ModelResponse(parts=[TextPart(f"{name} did: {step}")])


def runtime_for(session, cipher, model: Scripted) -> SimpleNamespace:
    pricing = Pricing.load(PLANS_FILE)
    function_model = FunctionModel(model.respond)
    pricing.models[function_model.model_name] = ModelPrice(input_per_mtok=1, output_per_mtok=1)
    models = SimpleNamespace(
        for_tier=lambda tier: function_model,
        byok=SimpleNamespace(model=lambda provider, name, key: function_model),
    )
    return SimpleNamespace(
        pricing=pricing,
        models=models,
        cipher=cipher,
        http=httpx.AsyncClient(),
        tavily_api_key=None,
        sessionmaker=None,  # the data tools only use it when they act, not to list collections
    )


async def chain_of(session, bot: Bot, peer: Bot) -> Task:
    task = Task(
        bot_id=bot.id,
        user_id=bot.owner_id,
        summary="digest",
        instruction="digest",
        schedule_kind=ScheduleKind.CRON,
        cron="0 8 * * *",
        timezone="UTC",
        destination_chat_id=OWNER,
        steps=[
            {"instruction": "Get the news", "bot_id": None},
            {"instruction": "Summarize it", "bot_id": bot.id},
            {"instruction": "Translate to English", "bot_id": peer.id},
        ],
    )
    session.add(task)
    await session.flush()
    return task


async def test_chain_feeds_each_output_to_the_next_step(session, cipher) -> None:
    bot = await make_bot(session, cipher, 1, "news_bot")
    peer = await make_bot(session, cipher, 2, "fr_bot")
    task = await chain_of(session, bot, peer)
    model = Scripted()
    runtime = runtime_for(session, cipher, model)

    text = await _run_chain(session, runtime, task, bot, bot.owner, NOW, ChatScope(bot.id, OWNER))

    assert text == "fr_bot did: Translate to English"
    assert [name for name, _ in model.prompts] == ["news_bot", "news_bot", "fr_bot"]
    assert "news_bot did: Get the news" in model.prompts[1][1]
    assert "news_bot did: Summarize it" in model.prompts[2][1]
    billed = list(await session.scalars(select(UsageEvent.bot_id).order_by(UsageEvent.id)))
    assert billed == [bot.id, bot.id, peer.id]


async def test_failing_step_names_the_step_and_bot(session, cipher) -> None:
    bot = await make_bot(session, cipher, 1, "news_bot")
    peer = await make_bot(session, cipher, 2, "fr_bot")
    await use_own_key(session, cipher, peer.owner, peer, Provider.OPENAI, "gpt-5-mini")
    task = await chain_of(session, bot, peer)
    runtime = runtime_for(session, cipher, Scripted(fail_for="fr_bot"))

    with pytest.raises(StepFailed) as failed:
        await _run_chain(session, runtime, task, bot, bot.owner, NOW, ChatScope(bot.id, OWNER))

    assert (failed.value.index, failed.value.username) == (2, "fr_bot")
    assert _error_text(failed.value, "en") == (
        "step 3 (@fr_bot): the key is invalid or was revoked"
    )


async def test_peer_steps_check_availability_and_quota(session, cipher) -> None:
    bot = await make_bot(session, cipher, 1, "news_bot")
    peer = await make_bot(session, cipher, 2, "fr_bot")
    task = await chain_of(session, bot, peer)
    model = Scripted()
    runtime = runtime_for(session, cipher, model)

    peer.status = BotStatus.SUSPENDED
    with pytest.raises(StepFailed, match="not available"):
        await _run_chain(session, runtime, task, bot, bot.owner, NOW, ChatScope(bot.id, OWNER))

    peer.status = BotStatus.ACTIVE
    runtime.pricing.plans["free"].daily_llm_messages = 2  # the two own steps use it up
    with pytest.raises(StepFailed) as failed:
        await _run_chain(session, runtime, task, bot, bot.owner, NOW, ChatScope(bot.id, OWNER))
    assert failed.value.reason == "quota: daily_messages"
    assert [name for name, _ in model.prompts].count("fr_bot") == 0


async def test_steps_of_another_owners_bot_never_run(session, cipher) -> None:
    bot = await make_bot(session, cipher, 1, "news_bot")
    stranger = await make_bot(session, cipher, 3, "their_bot", owner=2002)
    task = await chain_of(session, bot, stranger)
    model = Scripted()

    with pytest.raises(StepFailed, match="not available"):
        await _run_chain(
            session,
            runtime_for(session, cipher, model),
            task,
            bot,
            bot.owner,
            NOW,
            ChatScope(bot.id, OWNER),
        )
    assert "their_bot" not in [name for name, _ in model.prompts]


async def test_a_peer_first_step_checks_its_quota(session, cipher) -> None:
    bot = await make_bot(session, cipher, 1, "news_bot")
    peer = await make_bot(session, cipher, 2, "fr_bot")
    task = await chain_of(session, bot, peer)
    task.steps = [task.steps[2], task.steps[0]]  # the peer goes first
    model = Scripted()
    runtime = runtime_for(session, cipher, model)
    runtime.pricing.plans["free"].daily_llm_messages = 0

    with pytest.raises(StepFailed) as failed:
        await _run_chain(session, runtime, task, bot, bot.owner, NOW, ChatScope(bot.id, OWNER))

    assert (failed.value.index, failed.value.reason) == (0, "quota: daily_messages")
    assert model.prompts == []


async def test_peer_steps_cannot_delegate(session, cipher) -> None:
    bot = await make_bot(session, cipher, 1, "news_bot")
    peer = await make_bot(session, cipher, 2, "fr_bot")
    task = await chain_of(session, bot, peer)
    model = Scripted()
    runtime = runtime_for(session, cipher, model)
    runtime.sessionmaker = None  # Delegator is built, never called

    await _run_chain(session, runtime, task, bot, bot.owner, NOW, ChatScope(bot.id, OWNER))

    assert dict(model.tools) == {"news_bot": ["ask_bot"], "fr_bot": []}
