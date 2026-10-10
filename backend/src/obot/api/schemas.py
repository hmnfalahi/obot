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
    """One of the user's own tasks (SPEC 8.7)."""

    id: int
    bot_id: int
    bot_username: str
    summary: str
    instruction: str
    schedule_kind: str
    cron: str | None
    timezone: str
    destination_chat_id: int
    # Sent to the user's own chat with the bot; else to destination_title's group or channel.
    to_me: bool
    destination_title: str | None
    status: str
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_error: str | None
    steps: list[StepOut]


class WalletKeyOut(BaseModel):
    provider: Provider
    key_hint: str


class WalletKeyIn(BaseModel):
    api_key: str = Field(min_length=8, max_length=512)


class MySettingsOut(BaseModel):
    """One person's own settings for a bot they use (SPEC 8.7)."""

    bot_id: int
    username: str
    display_name: str
    is_owner: bool
    # What the owner recommends: Obot's tier, and a model for people's own keys.
    model_tier: str
    recommended_model: str | None
    key_provider: Provider | None
    model: str | None
    wallet: list[Provider]


class MySettingsIn(BaseModel):
    # None: answer with Obot's models (no own key).
    key_provider: Provider | None = None
    model: str | None = Field(default=None, pattern=f"^{MODEL_NAME.pattern}$")


class BotDetail(BotSummary):
    system_prompt: str
    language: str
    model_tier: str
    recommended_model: str | None
    allowed_users: list[AllowedUserOut]
    # How many people use the bot and in how many groups; never what they say (SPEC 8.7).
    users_count: int
    groups_count: int
    chats: list[ChatOut]


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
    recommended_model: str | None = Field(default=None, max_length=128)
    visibility: Literal["private", "public"] | None = None
    # People to keep sharing a private bot with; adding people goes through the factory.
    allowed_user_ids: list[int] | None = None


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
