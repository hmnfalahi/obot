"""Planner agent: turns task sentences from a draft into structured schedules."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models import Model

INSTRUCTIONS = """You convert task requests for a Telegram bot into triggers.

For each request return exactly one task:
- instruction: what the bot must do when the task runs, as a self-contained imperative in
  English (include URLs, topics and format the user asked for).
- trigger, one of:
  - 'schedule': at times. Set schedule 'cron' with a standard 5-field cron expression for
    recurring tasks, or 'once' with run_at as a local ISO datetime (YYYY-MM-DDTHH:MM).
  - 'rss': when a feed at `url` has a new item.
  - 'page_change': when the text of the page at `url` changes.
  - 'price': when a number on the page at `url` crosses `threshold`; `what` names the
    number precisely including the unit the user used (e.g. 'free-market USD rate in
    toman'), `threshold` is in that same unit, and `direction` is 'above' or 'below'.
  - 'group_keyword': when someone writes any of `keywords` in the group `source_chat`.
  - 'new_member': when someone joins the group `source_chat`.
  - 'webhook': when another service calls the task's web address.
  For rss, page_change and price set `every_minutes`, how often to check (default 60).
- timezone: an IANA name. Use the one the user mentioned, otherwise the owner's timezone.
- destination: 'owner' for the owner's private chat (the default), or the id of one of the
  bot's chats when the user asked to post there.
- summary: a short human description of the trigger in the user's language,
  e.g. 'every day at 8:00' / 'هر روز ساعت ۸ صبح' / 'وقتی دلار از ۱۰۰ هزار تومان رد شد'.
- steps: only for a chain, i.e. a request made of several different jobs done one after
  another (e.g. 'get the news, summarize it, translate it, send it') or that names another
  of the owner's bots for part of the work. List the steps in order; each has an
  instruction (self-contained, English) and bot: 'self' or one of the owner's other bots
  by @username. Each step gets the previous step's output, only the last step's output is
  sent. Then `instruction` is a one-line overview. Leave steps empty for a single job.
  A chain still needs its trigger, schedule, cron and the other fields like any task."""

TriggerName = Literal[
    "schedule", "rss", "page_change", "price", "group_keyword", "new_member", "webhook"
]


class PlannedStep(BaseModel):
    instruction: str
    bot: str = Field(default="self", description="'self' or @username of another bot")


class PlannedTask(BaseModel):
    instruction: str
    trigger: TriggerName = "schedule"
    schedule: Literal["cron", "once"] | None = None
    cron: str | None = None
    run_at: str | None = None
    url: str | None = None
    every_minutes: int | None = None
    what: str | None = None
    threshold: float | None = None
    direction: Literal["above", "below"] | None = None
    keywords: list[str] = Field(default_factory=list)
    source_chat: str | None = Field(default=None, description="a chat id from the list")
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
