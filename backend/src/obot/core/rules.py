"""Group reply rules (SPEC 3.3, phase 2): when a bot answers without being addressed.

Matching is two-stage so most group messages cost nothing:
1. keyword, regex and new_member rules match locally; topic rules compare
   embeddings (very cheap) against the rule's description;
2. only topic candidates above the similarity threshold go to a cheap yes/no
   classifier model.
"""

import enum
import math
from collections.abc import Sequence
from dataclasses import dataclass

import regex
from pydantic import BaseModel, Field, field_validator
from pydantic_ai import Agent
from pydantic_ai.models import Model

MAX_RULES = 10
MAX_PATTERN_LENGTH = 200
# User-written patterns can backtrack catastrophically; never let one stall a reply.
REGEX_TIMEOUT_SECONDS = 0.05
# Cosine similarity a message needs to a topic description before the classifier runs.
TOPIC_SIMILARITY_THRESHOLD = 0.45


class RuleKind(enum.StrEnum):
    KEYWORD = "keyword"
    REGEX = "regex"
    NEW_MEMBER = "new_member"
    TOPIC = "topic"


class ReplyRule(BaseModel):
    kind: RuleKind
    # keyword: any of these words; regex: one pattern; topic: a description of the topic.
    keywords: list[str] = Field(default_factory=list, max_length=20)
    pattern: str | None = Field(default=None, max_length=MAX_PATTERN_LENGTH)
    topic: str | None = Field(default=None, max_length=300)
    instruction: str = Field(
        default="", max_length=500, description="How to reply when this rule matches."
    )

    @field_validator("pattern")
    @classmethod
    def compiles(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                regex.compile(value)
            except regex.error as e:
                raise ValueError(f"invalid pattern: {e}") from e
        return value

    def is_complete(self) -> bool:
        match self.kind:
            case RuleKind.KEYWORD:
                return any(k.strip() for k in self.keywords)
            case RuleKind.REGEX:
                return bool(self.pattern)
            case RuleKind.TOPIC:
                return bool(self.topic and self.topic.strip())
            case RuleKind.NEW_MEMBER:
                return True


def parse_rules(raw: Sequence[dict]) -> list[ReplyRule]:
    """Stored rules, skipping any that no longer validate."""
    rules = []
    for item in raw[:MAX_RULES]:
        try:
            rule = ReplyRule.model_validate(item)
        except ValueError:
            continue
        if rule.is_complete():
            rules.append(rule)
    return rules


def matches_locally(rule: ReplyRule, text: str) -> bool:
    """Stage 1 for keyword and regex rules (topic and new_member are handled elsewhere)."""
    if rule.kind == RuleKind.KEYWORD:
        lowered = text.casefold()
        return any(k.strip() and k.strip().casefold() in lowered for k in rule.keywords)
    if rule.kind == RuleKind.REGEX and rule.pattern:
        try:
            return regex.search(rule.pattern, text, timeout=REGEX_TIMEOUT_SECONDS) is not None
        except TimeoutError:
            return False
    return False


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a) * sum(y * y for y in b))
    return dot / norm if norm else 0.0


class Verdict(BaseModel):
    relevant: bool


@dataclass(frozen=True)
class Classification:
    relevant: bool
    model_name: str
    input_tokens: int
    output_tokens: int


async def classify(model: Model, topic: str, text: str) -> Classification:
    """Stage 2 for topic rules: does the message belong to the topic?"""
    agent = Agent(
        model,
        output_type=Verdict,
        instructions=(
            "Decide whether a Telegram group message is about the given topic and calls for "
            "a reply from a bot that covers that topic. Small talk, jokes and messages to "
            "other people are not relevant."
        ),
    )
    result = await agent.run(f"Topic: {topic}\n\nMessage: {text}")
    return Classification(
        relevant=result.output.relevant,
        model_name=model.model_name,
        input_tokens=result.usage.input_tokens,
        output_tokens=result.usage.output_tokens,
    )


def rule_note(rule: ReplyRule) -> str:
    """Added to the bot's instructions when it replies because of a rule."""
    reason = {
        RuleKind.KEYWORD: "the message contains one of your keywords",
        RuleKind.REGEX: "the message matches one of your patterns",
        RuleKind.TOPIC: f"the message is about: {rule.topic}",
        RuleKind.NEW_MEMBER: "a new member joined the group; welcome them",
    }[rule.kind]
    text = (
        f"Nobody mentioned you; you are replying because {reason}. Answer only the latest "
        "message: earlier messages in the history were not meant for you, so do not reply "
        "to or greet them."
    )
    if rule.instruction:
        text += f" {rule.instruction}"
    return text
