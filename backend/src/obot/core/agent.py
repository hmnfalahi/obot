"""Platform-independent agent: turns a bot profile and chat history into a reply."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic_ai import Agent, Tool, UsageLimits
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models import Model

from obot.core.tools import ToolServices

DEFAULT_INSTRUCTIONS = "You are a helpful, friendly assistant."

PLATFORM_RULES = (
    "You are chatting on Telegram. Reply in the language of the user's latest message. "
    "Keep replies concise. Use plain text only: no Markdown, no tables. "
    "Use your tools when the answer needs current or external information. "
    "If a tool returns an ERROR, tell the user plainly what failed; never invent data."
)

# Model requests per reply, including tool round-trips; bounds cost of tool loops.
REQUEST_LIMIT = 6


@dataclass(frozen=True)
class BotProfile:
    name: str
    persona: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Turn:
    role: Literal["user", "assistant"]
    text: str


@dataclass(frozen=True)
class Reply:
    text: str
    model_name: str
    input_tokens: int
    output_tokens: int


def build_instructions(profile: BotProfile, now: datetime | None = None) -> str:
    custom = (profile.persona.get("system_prompt") or "").strip() or DEFAULT_INSTRUCTIONS
    now = now or datetime.now(UTC)
    return (
        f"Your name is {profile.name}.\n\n{custom}\n\n{PLATFORM_RULES}\n\n"
        f"Current date and time: {now:%Y-%m-%d %H:%M} UTC."
    )


def to_model_messages(history: Sequence[Turn]) -> list[ModelMessage]:
    messages: list[ModelMessage] = []
    for turn in history:
        if turn.role == "user":
            messages.append(ModelRequest(parts=[UserPromptPart(content=turn.text)]))
        else:
            messages.append(ModelResponse(parts=[TextPart(content=turn.text)]))
    return messages


async def generate_reply(
    model: Model,
    profile: BotProfile,
    history: Sequence[Turn],
    user_text: str,
    tools: Sequence[Tool[ToolServices]] = (),
    services: ToolServices | None = None,
) -> Reply:
    agent = Agent(
        model, instructions=build_instructions(profile), tools=tools, deps_type=ToolServices
    )
    result = await agent.run(
        user_text,
        message_history=to_model_messages(history),
        deps=services,
        usage_limits=UsageLimits(request_limit=REQUEST_LIMIT),
    )
    usage = result.usage
    return Reply(
        text=result.output,
        model_name=model.model_name,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
    )
