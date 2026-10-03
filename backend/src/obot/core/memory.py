"""Long-term memory: extracting durable facts from chat messages and embedding them."""

from collections.abc import Sequence
from dataclasses import dataclass

from openai import AsyncOpenAI
from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models import Model

INSTRUCTIONS = """You maintain the long-term memory of a Telegram bot for one chat.

From the new messages, extract durable facts worth remembering in future conversations:
people's names, roles, preferences, plans, decisions, recurring events, and facts users
explicitly ask the bot to remember. Ignore small talk, questions, one-off requests and
anything the bot itself said unless a user confirmed it.

Write each fact as one short self-contained sentence naming who it is about, in the
language the chat uses. Do not repeat facts that are already known. If a new message
shows an existing fact is wrong or outdated, list that fact's id in `remove` and add the
corrected fact. Return empty lists when there is nothing worth remembering."""


class MemoryUpdate(BaseModel):
    add: list[str] = Field(default_factory=list, description="New durable facts.")
    remove: list[int] = Field(
        default_factory=list, description="Ids of existing facts that are wrong or outdated."
    )


@dataclass(frozen=True)
class KnownFact:
    id: int
    fact: str


@dataclass(frozen=True)
class ExtractionResult:
    update: MemoryUpdate
    model_name: str
    input_tokens: int
    output_tokens: int


def build_prompt(known: Sequence[KnownFact], messages: Sequence[str]) -> str:
    facts = "\n".join(f"[{f.id}] {f.fact}" for f in known) or "(none)"
    lines = "\n".join(messages)
    return f"Known facts:\n{facts}\n\nNew messages:\n{lines}"


async def extract_facts(
    model: Model, known: Sequence[KnownFact], messages: Sequence[str]
) -> ExtractionResult:
    agent = Agent(model, output_type=MemoryUpdate, instructions=INSTRUCTIONS)
    result = await agent.run(build_prompt(known, messages))
    known_ids = {f.id for f in known}
    update = result.output
    # The model may hallucinate ids; only ids it was shown can be removed.
    update.remove = [i for i in update.remove if i in known_ids]
    update.add = [fact.strip() for fact in update.add if fact.strip()]
    return ExtractionResult(
        update=update,
        model_name=model.model_name,
        input_tokens=result.usage.input_tokens,
        output_tokens=result.usage.output_tokens,
    )


@dataclass(frozen=True)
class Embeddings:
    vectors: list[list[float]]
    model_name: str
    tokens: int


class Embedder:
    def __init__(self, client: AsyncOpenAI, model: str) -> None:
        self._client = client
        self.model = model

    async def embed(self, texts: Sequence[str]) -> Embeddings:
        if not texts:
            return Embeddings([], self.model, 0)
        response = await self._client.embeddings.create(model=self.model, input=list(texts))
        ordered = sorted(response.data, key=lambda d: d.index)
        return Embeddings(
            vectors=[d.embedding for d in ordered],
            model_name=self.model,
            tokens=response.usage.prompt_tokens if response.usage else 0,
        )
