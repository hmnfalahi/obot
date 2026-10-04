import enum
from datetime import datetime
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    LargeBinary,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {datetime: DateTime(timezone=True)}


class BotStatus(enum.StrEnum):
    ACTIVE = "active"
    # Not served because the owner's plan does not allow it (e.g. too many bots).
    SUSPENDED = "suspended"
    # Turned off by a service admin (kill switch).
    DISABLED = "disabled"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tg_user_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    language: Mapped[str | None] = mapped_column(String(16))
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    plan: Mapped[str] = mapped_column(String(32), default="free")
    credit_balance: Mapped[int] = mapped_column(BigInteger, default=0)
    is_disabled: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    bots: Mapped[list["Bot"]] = relationship(back_populates="owner")


class Bot(Base):
    __tablename__ = "bots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    tg_bot_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    username: Mapped[str] = mapped_column(String(64))
    display_name: Mapped[str] = mapped_column(String(128))
    # AES-GCM, bound to tg_bot_id; see obot.crypto
    token_encrypted: Mapped[bytes] = mapped_column(LargeBinary)
    persona: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    model_tier: Mapped[str] = mapped_column(String(32), default="cheap")
    # Bring your own key: replies use the owner's provider account instead of model_tier.
    # The key is AES-GCM encrypted, bound to the bot id; see obot.bots.byok.
    byok_provider: Mapped[str | None] = mapped_column(String(16))
    byok_model: Mapped[str | None] = mapped_column(String(128))
    byok_key_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    tools: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    # Group reply rules; see obot.core.rules
    reply_rules: Mapped[list[Any]] = mapped_column(JSONB, default=list, server_default="[]")
    daily_credit_cap: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[BotStatus] = mapped_column(
        Enum(
            BotStatus,
            native_enum=False,
            length=16,
            values_callable=lambda e: [m.value for m in e],
        ),
        default=BotStatus.ACTIVE,
    )
    # Telegram does not let a bot message its creator until they press Start.
    owner_started: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    owner: Mapped[User] = relationship(back_populates="bots")


class SenderKind(enum.StrEnum):
    HUMAN = "human"
    BOT = "bot"  # another bot in the chat
    SELF = "self"  # this bot's own reply


class Message(Base):
    """Raw chat history, kept for 30 days. Always accessed through obot.chat.history."""

    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_scope_created", "bot_id", "tg_chat_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id", ondelete="CASCADE"))
    tg_chat_id: Mapped[int] = mapped_column(BigInteger)
    tg_message_id: Mapped[int | None] = mapped_column(BigInteger)
    sender_kind: Mapped[SenderKind] = mapped_column(
        Enum(
            SenderKind,
            native_enum=False,
            length=16,
            values_callable=lambda e: [m.value for m in e],
        )
    )
    sender_tg_id: Mapped[int | None] = mapped_column(BigInteger)
    sender_name: Mapped[str | None] = mapped_column(String(256))
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class UsageKind(enum.StrEnum):
    LLM = "llm"
    SEARCH = "search"
    FETCH = "fetch"
    # Background memory extraction and embeddings; not counted as messages.
    MEMORY = "memory"
    API = "api"  # a call to an owner-defined endpoint (call_api)


class ApiEndpoint(Base):
    """An HTTP endpoint of the owner that a bot may call; see obot.core.custom_api."""

    __tablename__ = "api_endpoints"
    __table_args__ = (UniqueConstraint("bot_id", "name"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(40))
    description: Mapped[str] = mapped_column(String(500))
    method: Mapped[str] = mapped_column(String(8))
    url: Mapped[str] = mapped_column(String(2048))
    # JSON object of secret headers, AES-GCM encrypted and bound to the bot; see obot.bots.apis
    headers_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class UsageEvent(Base):
    __tablename__ = "usage_events"
    __table_args__ = (
        Index("ix_usage_events_owner_created", "owner_id", "created_at"),
        Index("ix_usage_events_bot_created", "bot_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id", ondelete="CASCADE"))
    kind: Mapped[UsageKind] = mapped_column(
        Enum(
            UsageKind,
            native_enum=False,
            length=16,
            values_callable=lambda e: [m.value for m in e],
        )
    )
    model: Mapped[str | None] = mapped_column(String(128))
    tokens_in: Mapped[int] = mapped_column(BigInteger, default=0)
    tokens_out: Mapped[int] = mapped_column(BigInteger, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(14, 8))
    credits: Mapped[int] = mapped_column(BigInteger)
    # Paid with the owner's own key: free for them and not counted against plan limits.
    byok: Mapped[bool] = mapped_column(default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class DraftKind(enum.StrEnum):
    NEW = "new"
    EDIT = "edit"


class DraftState(enum.StrEnum):
    OPEN = "open"
    # "Create" pressed; waiting for Telegram's managed_bot update.
    AWAITING_CREATION = "awaiting_creation"
    APPLIED = "applied"
    CANCELLED = "cancelled"


class FactoryDraft(Base):
    """A bot design being discussed with the factory bot. See obot.factory.drafts."""

    __tablename__ = "factory_drafts"
    __table_args__ = (Index("ix_factory_drafts_user_state", "user_id", "state"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # Target bot for edits; the created bot for applied new drafts.
    bot_id: Mapped[int | None] = mapped_column(ForeignKey("bots.id", ondelete="CASCADE"))
    kind: Mapped[DraftKind] = mapped_column(
        Enum(
            DraftKind,
            native_enum=False,
            length=16,
            values_callable=lambda e: [m.value for m in e],
        )
    )
    state: Mapped[DraftState] = mapped_column(
        Enum(
            DraftState,
            native_enum=False,
            length=32,
            values_callable=lambda e: [m.value for m in e],
        ),
        default=DraftState.OPEN,
    )
    draft: Mapped[dict[str, Any]] = mapped_column(JSONB)
    suggested_username: Mapped[str | None] = mapped_column(String(64))
    # Designer's last message, so answers to its questions keep their context.
    last_reply: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class BotChat(Base):
    """Groups and channels a bot is (or was) a member of."""

    __tablename__ = "bot_chats"
    __table_args__ = (UniqueConstraint("bot_id", "tg_chat_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id", ondelete="CASCADE"))
    tg_chat_id: Mapped[int] = mapped_column(BigInteger)
    chat_type: Mapped[str] = mapped_column(String(16))
    title: Mapped[str | None] = mapped_column(String(256))
    is_member: Mapped[bool] = mapped_column(default=True)
    is_admin: Mapped[bool] = mapped_column(default=False)
    joined_at: Mapped[datetime] = mapped_column(server_default=func.now())
    left_at: Mapped[datetime | None]
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


EMBEDDING_DIMENSIONS = 1024


class ChatMemory(Base):
    """A durable fact a bot learned in one chat. Always accessed through obot.memory.store."""

    __tablename__ = "chat_memories"
    __table_args__ = (Index("ix_chat_memories_scope", "bot_id", "tg_chat_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id", ondelete="CASCADE"))
    tg_chat_id: Mapped[int] = mapped_column(BigInteger)
    fact: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class MemoryCursor(Base):
    """Last message of a chat already processed by memory extraction."""

    __tablename__ = "memory_cursors"

    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id", ondelete="CASCADE"), primary_key=True)
    tg_chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    last_message_id: Mapped[int] = mapped_column(BigInteger)


class TaskStatus(enum.StrEnum):
    ACTIVE = "active"
    # Destination is the owner's private chat and they have not pressed Start yet.
    WAITING_START = "waiting_start"
    PAUSED = "paused"
    DONE = "done"  # one-off task that ran
    CANCELLED = "cancelled"  # replaced by a newer configuration


class ScheduleKind(enum.StrEnum):
    CRON = "cron"
    ONCE = "once"


class TriggerKind(enum.StrEnum):
    SCHEDULE = "schedule"  # cron or one-off time
    # Watchers: polled every trigger_config["every_minutes"] minutes.
    RSS = "rss"  # a new item in a feed
    PAGE_CHANGE = "page_change"  # the text of a page changed
    PRICE = "price"  # a number on a page crossed a threshold
    # Push: fired by an event as it happens.
    GROUP_KEYWORD = "group_keyword"
    NEW_MEMBER = "new_member"
    WEBHOOK = "webhook"


WATCHER_TRIGGERS = (TriggerKind.RSS, TriggerKind.PAGE_CHANGE, TriggerKind.PRICE)


class Task(Base):
    """A job of a bot: when its trigger fires, the agent follows `instruction`."""

    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_due", "status", "next_run_at"),
        Index("ix_tasks_bot", "bot_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id", ondelete="CASCADE"))
    instruction: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(String(256))
    trigger: Mapped[TriggerKind] = mapped_column(
        Enum(
            TriggerKind,
            native_enum=False,
            length=16,
            values_callable=lambda e: [m.value for m in e],
        ),
        default=TriggerKind.SCHEDULE,
        server_default=TriggerKind.SCHEDULE.value,
    )
    # Trigger parameters (url, keywords, chat_id, threshold...) and what watchers
    # remember between polls (seen feed items, page hash, last price side).
    trigger_config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    trigger_state: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    # Secret part of the webhook URL of WEBHOOK tasks.
    hook_secret: Mapped[str | None] = mapped_column(String(64))
    # Only for SCHEDULE tasks.
    schedule_kind: Mapped[ScheduleKind | None] = mapped_column(
        Enum(
            ScheduleKind,
            native_enum=False,
            length=16,
            values_callable=lambda e: [m.value for m in e],
        )
    )
    cron: Mapped[str | None] = mapped_column(String(128))
    run_at: Mapped[datetime | None]
    timezone: Mapped[str] = mapped_column(String(64))
    destination_chat_id: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[TaskStatus] = mapped_column(
        Enum(
            TaskStatus,
            native_enum=False,
            length=16,
            values_callable=lambda e: [m.value for m in e],
        ),
        default=TaskStatus.ACTIVE,
    )
    next_run_at: Mapped[datetime | None]
    last_run_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class TaskRun(Base):
    __tablename__ = "task_runs"
    __table_args__ = (Index("ix_task_runs_task", "task_id", "started_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    ok: Mapped[bool]
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ReportStatus(enum.StrEnum):
    OPEN = "open"
    ACTIONED = "actioned"  # the bot was disabled
    DISMISSED = "dismissed"


class Report(Base):
    """An abuse report about a bot, reviewed by service admins."""

    __tablename__ = "reports"
    __table_args__ = (Index("ix_reports_status", "status", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id", ondelete="CASCADE"))
    reporter_tg_id: Mapped[int] = mapped_column(BigInteger)
    tg_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[ReportStatus] = mapped_column(
        Enum(
            ReportStatus,
            native_enum=False,
            length=16,
            values_callable=lambda e: [m.value for m in e],
        ),
        default=ReportStatus.OPEN,
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    resolved_at: Mapped[datetime | None]


class Payment(Base):
    """Credits bought with Telegram Stars or granted by an admin after a manual payment."""

    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # "stars" or "manual" (card-to-card, granted by an admin)
    method: Mapped[str] = mapped_column(String(16), default="stars", server_default="stars")
    # Admin who granted a manual payment, and their note (e.g. the receipt reference).
    granted_by: Mapped[int | None] = mapped_column(BigInteger)
    note: Mapped[str | None] = mapped_column(Text)
    package_id: Mapped[str] = mapped_column(String(32))
    stars: Mapped[int]
    credits: Mapped[int] = mapped_column(BigInteger)
    # Telegram's id of the charge (or "manual-<uuid>"); unique so a redelivered update
    # or a double-tapped grant button cannot credit twice.
    telegram_charge_id: Mapped[str] = mapped_column(String(128), unique=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    refunded_at: Mapped[datetime | None]


class TaskEvent(Base):
    """A fired trigger waiting to be run, from any process (bots, API webhooks, watchers)."""

    __tablename__ = "task_events"
    __table_args__ = (Index("ix_task_events_pending", "processed_at", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    payload: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    processed_at: Mapped[datetime | None]
