"""Planner agent: turns task sentences from a draft into structured schedules."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models import Model

INSTRUCTIONS = """You convert task requests for a Telegram bot into schedules.

For each request return exactly one task:
- instruction: what the bot must do when the task runs, as a self-contained imperative in
  English (include URLs, topics and format the user asked for).
- schedule: 'cron' with a standard 5-field cron expression for recurring tasks, or 'once'
  with run_at as a local ISO datetime (YYYY-MM-DDTHH:MM).
- timezone: an IANA name. Use the one the user mentioned, otherwise the owner's timezone.
- destination: 'owner' for the owner's private chat (the default), or the id of one of the
  bot's chats when the user asked to post there.
- summary: a short human description of the schedule in the user's language,
  e.g. 'every day at 8:00' / 'هر روز ساعت ۸ صبح'.
- steps: only for a chain, i.e. a request made of several different jobs done one after
  another (e.g. 'get the news, summarize it, translate it, send it') or that names another
  of the owner's bots for part of the work. List the steps in order; each has an
  instruction (self-contained, English) and bot: 'self' or one of the owner's other bots
  by @username. Each step gets the previous step's output, only the last step's output is
  sent. Then `instruction` is a one-line overview. Leave steps empty for a single job.
  A chain still needs its schedule, cron and the other fields like any task."""


class PlannedStep(BaseModel):
    instruction: str
    bot: str = Field(default="self", description="'self' or @username of another bot")


class PlannedTask(BaseModel):
    instruction: str
    schedule: Literal["cron", "once"] | None = None
    cron: str | None = None
    run_at: str | None = None
    timezone: str
    destination: str = Field(description="'owner' or a chat id from the list")
    summary: str = Field(max_length=200)
    steps: list[PlannedStep] = Field(default_factory=list)


class TaskPlan(BaseModel):
    tasks: list[PlannedTask]


@dataclass(frozen=True)
class KnownChat:
    id: int
    title: str | None
    type: str


@dataclass(frozen=True)
class KnownBot:
    """Another bot of the same owner that chain steps may use."""

    id: int
    username: str
    description: str


@dataclass(frozen=True)
class PlanResult:
    plan: TaskPlan
    model_name: str
    input_tokens: int
    output_tokens: int


def build_prompt(
    requests: Sequence[str],
    timezone: str,
    chats: Sequence[KnownChat],
    now: datetime,
    peers: Sequence[KnownBot] = (),
) -> str:
    chat_lines = "\n".join(f"- {c.id}: {c.title or ''} ({c.type})" for c in chats) or "(none)"
    peer_lines = "\n".join(f"- @{b.username}: {b.description}" for b in peers) or "(none)"
    request_lines = "\n".join(f"{i + 1}. {r}" for i, r in enumerate(requests))
    return (
        f"Now: {now:%Y-%m-%d %H:%M} UTC\nOwner's timezone: {timezone}\n"
        f"Bot's chats:\n{chat_lines}\nOwner's other bots:\n{peer_lines}\n\n"
        f"Requests:\n{request_lines}"
    )


async def plan_tasks(
    model: Model,
    requests: Sequence[str],
    timezone: str,
    chats: Sequence[KnownChat],
    now: datetime,
    peers: Sequence[KnownBot] = (),
) -> PlanResult:
    agent = Agent(model, output_type=TaskPlan, instructions=INSTRUCTIONS)
    result = await agent.run(build_prompt(requests, timezone, chats, now, peers))
    return PlanResult(
        plan=result.output,
        model_name=model.model_name,
        input_tokens=result.usage.input_tokens,
        output_tokens=result.usage.output_tokens,
    )
