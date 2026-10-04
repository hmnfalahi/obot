from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from obot.core.rules import MAX_RULES, ReplyRule
from obot.factory.designer import ToolName


class Quota(BaseModel):
    used: int
    limit: int | None


class MeOut(BaseModel):
    tg_user_id: int
    model_tiers: list[str]
    language: Literal["fa", "en"]
    timezone: str
    plan: str
    credit_balance: int
    max_bots: int
    max_tasks: int
    messages_today: Quota
    searches_today: Quota


class MeUpdate(BaseModel):
    language: Literal["fa", "en"] | None = None
    timezone: str | None = None


class BotSummary(BaseModel):
    id: int
    username: str
    display_name: str
    status: str
    description: str
    tools: list[str]
    owner_started: bool


class ChatOut(BaseModel):
    id: int
    title: str | None
    type: str
    is_admin: bool


class TaskOut(BaseModel):
    id: int
    summary: str
    instruction: str
    schedule_kind: str
    cron: str | None
    timezone: str
    destination_chat_id: int
    to_owner: bool
    status: str
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_error: str | None


class BotDetail(BotSummary):
    reply_rules: list[ReplyRule]
    system_prompt: str
    language: str
    model_tier: str
    daily_credit_cap: int | None
    chats: list[ChatOut]
    tasks: list[TaskOut]


class PackageOut(BaseModel):
    id: str
    stars: int
    credits: int


class InvoiceIn(BaseModel):
    package_id: str


class InvoiceOut(BaseModel):
    link: str


class BotUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=120)
    system_prompt: str | None = Field(default=None, max_length=8000)
    language: str | None = Field(default=None, max_length=8)
    tools: list[ToolName] | None = None
    model_tier: Literal["cheap", "strong"] | None = None
    reply_rules: list[ReplyRule] | None = Field(default=None, max_length=MAX_RULES)
    daily_credit_cap: int | None = Field(default=None, ge=0)
    clear_daily_credit_cap: bool = False


class TaskRequest(BaseModel):
    request: str = Field(min_length=3, max_length=500)


class RejectedOut(BaseModel):
    summary: str
    reason: str


class TaskAddOut(BaseModel):
    scheduled: list[TaskOut]
    rejected: list[RejectedOut]


class TaskUpdate(BaseModel):
    paused: bool


class UsageDay(BaseModel):
    day: date
    messages: int
    credits: int


class NewBotIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=120)
    system_prompt: str = Field(min_length=1, max_length=8000)
    language: str = Field(default="fa", max_length=8)
    tools: list[ToolName] = Field(default_factory=list)


class NewBotOut(BaseModel):
    create_link: str
    suggested_username: str
