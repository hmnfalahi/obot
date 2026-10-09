"""Designer agent: turns a user's description into a bot draft (SPEC 3.2).

It guesses first and asks only for missing essentials, so a single message is
usually enough to get a complete draft.
"""

import enum
import json
import re
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelRetry
from pydantic_ai.models import Model

from obot.data.errors import InvalidSchema
from obot.factory.collections import CollectionDraft, to_schema


class ToolName(enum.StrEnum):
    WEB_SEARCH = "web_search"
    READ_URL = "read_url"
    RSS = "rss"
    MEMORY = "memory"
    PERSONAL_DATA = "personal_data"


TOOL_DESCRIPTIONS = {
    ToolName.WEB_SEARCH: "search the web for current information",
    ToolName.READ_URL: "read the content of a given web page (e.g. a price from a site)",
    ToolName.RSS: "read RSS/Atom feeds",
    ToolName.MEMORY: "remember facts about the chat over time",
    ToolName.PERSONAL_DATA: (
        "keep each user's own records they ask for in chat (expenses, weight, habits, lists)"
    ),
}


class BotDraft(BaseModel):
    name: str = Field(description="Display name of the bot, max 64 chars.", max_length=64)
    username_hint: str = Field(
        description="Short latin slug for the bot username, e.g. 'cryptobrief'. a-z, 0-9, _."
    )
    description: str = Field(
        description="One sentence shown on the bot's Telegram profile, in the user's language.",
        max_length=120,
    )
    system_prompt: str = Field(
        description=(
            "Instructions for the bot, written in English, in second person ('You are...'). "
            "Include personality, tone, what it does and how it answers."
        )
    )
    language: str = Field(description="Main language the bot speaks, as ISO 639-1 code.")
    tools: list[ToolName] = Field(default_factory=list)
    collections: list[CollectionDraft] = Field(
        default_factory=list,
        description=(
            "Lists the bot keeps (bookings, orders, expenses...). Empty for bots that only "
            "talk or fetch information."
        ),
    )


class DesignerOutput(BaseModel):
    action: Literal["draft", "chat"] = Field(
        description="'draft' when you produced or updated a bot draft, 'chat' otherwise."
    )
    target_bot: str | None = Field(
        default=None,
        description="Username of an existing bot the user wants to change, if any.",
    )
    draft: BotDraft | None = None
    questions: list[str] = Field(
        default_factory=list,
        max_length=3,
        description="Only questions whose answers are essential and cannot be guessed.",
    )
    reply: str = Field(description="Short message to the user, in the user's language.")


def check_output(output: DesignerOutput) -> None:
    """Ask the model to fix a draft whose collections obot.data would refuse."""
    draft = output.draft
    if draft is None:
        return
    keys = [c.key for c in draft.collections]
    if len(set(keys)) != len(keys):
        raise ModelRetry("collection keys must be unique within a bot")
    for c in draft.collections:
        try:
            to_schema(c, draft.language)
        except InvalidSchema as e:
            raise ModelRetry(f"fix collection {e}") from None


@dataclass(frozen=True)
class ExistingBot:
    username: str
    draft: BotDraft


@dataclass(frozen=True)
class DesignerResult:
    output: DesignerOutput
    model_name: str
    input_tokens: int
    output_tokens: int


def _instructions() -> str:
    tools = "\n".join(f"- {name.value}: {desc}" for name, desc in TOOL_DESCRIPTIONS.items())
    return f"""You design Telegram AI bots for users of Obot, a bot factory.

The user describes the bot they want, or a change to a draft or to one of their bots.
Produce a complete draft right away: guess sensible values instead of asking. Ask at most
3 questions, and only when an essential detail is missing and cannot be guessed (for example
which website to read a price from). Never ask about name, tone or language; pick them.

When a current draft is given, apply the user's message to it and return the full updated
draft. When the user refers to one of their existing bots, set target_bot to its username
and return its full updated draft. If the message is not about designing a bot, use
action 'chat' and briefly explain what you can do.

Available tools (only choose what the bot needs):
{tools}

In groups a bot only answers when it is mentioned or replied to. A bot is a persona and its
tools; schedules are not part of it. Everyone who uses a bot asks it, in its private chat,
for their own scheduled jobs (e.g. 'every day at 8:00 send me the gold price'), and the bot
asks them to confirm. When the user wants something on a schedule, design the bot that can
do it and mention in `reply` that they then just tell the bot when to do it. Scheduled jobs
only run at set times: a bot cannot react to events such as new feed items, page or price
changes or people joining; offer a regular check instead (e.g. 'every hour, check the price
and tell me if it is above X').

When the bot must keep things people send (bookings, orders, registrations, expenses,
lists), add collections to the draft. 'shared' collections collect from everyone and only
the owner sees all records (bookings, orders, sign-ups); mark only the fields that show a
slot is taken as public (e.g. date and time), never names, phones or notes, and make
unique rules only over public fields (e.g. [['date','time']] so a slot is booked once). Add
allowed_hours when bookings are only possible at certain times (weekdays 0 = Monday …
5 = Saturday, 6 = Sunday). 'personal' collections give each user their own records
(expense book, habit tracker). If users should invent their own lists in chat, add the
personal_data tool instead. Keys are lowercase latin slugs; titles and labels are in the
bot's language. Collections cannot notify or remind anyone yet: never promise that. When
changing an existing bot, keep its collections and their keys unless the user asks
otherwise; leaving one out never deletes its data.

Write `reply`, `questions` and `description` in the user's language. Keep `reply` to one or
two sentences; the draft itself is shown to the user separately."""


def build_prompt(
    message: str, current: BotDraft | None, existing: Sequence[ExistingBot], last_reply: str | None
) -> str:
    parts = []
    if existing:
        bots = {b.username: b.draft.model_dump(mode="json") for b in existing}
        parts.append(f"The user's existing bots:\n{json.dumps(bots, ensure_ascii=False)}")
    if current is not None:
        parts.append(f"Current draft:\n{current.model_dump_json()}")
    if last_reply:
        parts.append(f"Your previous message to the user:\n{last_reply}")
    parts.append(f"User message:\n{message}")
    return "\n\n".join(parts)


def dated_prompt(prompt: str, now: datetime) -> str:
    return f"Now: {now:%Y-%m-%d %H:%M} UTC\n\n{prompt}"


async def design(
    model: Model,
    message: str,
    current: BotDraft | None = None,
    existing: Sequence[ExistingBot] = (),
    last_reply: str | None = None,
) -> DesignerResult:
    agent = Agent(model, output_type=DesignerOutput, instructions=_instructions())

    @agent.output_validator
    def _valid(output: DesignerOutput) -> DesignerOutput:
        check_output(output)
        return output

    prompt = build_prompt(message, current, existing, last_reply)
    result = await agent.run(dated_prompt(prompt, datetime.now(UTC)))
    return DesignerResult(
        output=result.output,
        model_name=model.model_name,
        input_tokens=result.usage.input_tokens,
        output_tokens=result.usage.output_tokens,
    )


def make_username(hint: str) -> str:
    """A Telegram-valid, probably-free username suggestion; the user can still edit it."""
    slug = re.sub(r"[^a-z0-9_]", "", hint.lower().replace("-", "_").replace(" ", "_"))
    slug = re.sub(r"_+", "_", slug).strip("_")
    slug = re.sub(r"_?bot$", "", slug)  # we append our own "_bot"
    if not slug or not slug[0].isalpha():
        slug = f"obot{slug}"
    return f"{slug[:20]}_{secrets.token_hex(2)}_bot"
