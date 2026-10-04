from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from obot.core.byok import MODEL_NAME, Provider
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
    max_apis: int
    messages_today: Quota
    searches_today: Quota
    # For t.me links that continue in the factory chat.
    factory_username: str


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
    visibility: Literal["private", "public"]


class UsedBotOut(BaseModel):
    """Another person's bot that the user started (SPEC 8.7)."""

    id: int
    username: str
    display_name: str
    description: str


class AllowedUserOut(BaseModel):
    id: int
    name: str


class ChatOut(BaseModel):
    id: int
    title: str | None
    type: str
    is_admin: bool


class StepOut(BaseModel):
    instruction: str
    # None: the task's own bot
    bot_username: str | None


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
    steps: list[StepOut]


class ByokOut(BaseModel):
    provider: Provider
    model: str
    key_hint: str


class ByokIn(BaseModel):
    provider: Provider
    model: str = Field(pattern=f"^{MODEL_NAME.pattern}$")
    # Omitted: keep the stored key (same provider only), e.g. to switch models.
    api_key: str | None = Field(default=None, min_length=8, max_length=512)


class ApiOut(BaseModel):
    id: int
    name: str
    description: str
    method: str
    url: str
    # Values are secret and never sent back.
    header_names: list[str]


class ApiIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    description: str = Field(min_length=1, max_length=500)
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    url: str = Field(min_length=8, max_length=2048)
    # Omitted on update: keep the stored headers.
    headers: dict[str, str] | None = None


class BotDetail(BotSummary):
    system_prompt: str
    language: str
    model_tier: str
    byok: ByokOut | None
    apis: list[ApiOut]
    daily_credit_cap: int | None
    allowed_users: list[AllowedUserOut]
    # How many people use the bot and in how many groups; never what they say (SPEC 8.7).
    users_count: int
    groups_count: int
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
    daily_credit_cap: int | None = Field(default=None, ge=0)
    clear_daily_credit_cap: bool = False
    visibility: Literal["private", "public"] | None = None
    # People to keep sharing a private bot with; adding people goes through the factory.
    allowed_user_ids: list[int] | None = None


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
