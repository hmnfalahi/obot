from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="OBOT_", extra="ignore")

    env: str = "dev"
    database_url: str = "postgresql+asyncpg://obot:obot@localhost:5433/obot"
    redis_url: str = "redis://localhost:6380/0"

    factory_bot_token: SecretStr | None = None
    # base64url-encoded 32-byte key for encrypting bot tokens at rest
    token_encryption_key: SecretStr | None = None
    openrouter_api_key: SecretStr | None = None
    # Web search for bots with the web_search tool; disabled when unset
    tavily_api_key: SecretStr | None = None
    # OpenRouter model ids per tier; see https://openrouter.ai/models
    model_cheap: str = "google/gemini-3.1-flash-lite"
    model_strong: str = "anthropic/claude-sonnet-5.5"
    max_output_tokens: int = 1024
    # Short-term memory: how many previous messages of a chat the agent sees
    history_limit: int = 20
    # Long-term memory
    embedding_model: str = "baai/bge-m3"
    memory_extract_interval_seconds: int = 300
    message_retention_days: int = 30

    plans_file: Path = Path("plans.yaml")
    # Designer (LLM) messages a user may send to the factory bot per day
    factory_daily_messages: int = 50

    # Optional LLM tracing; enabled when both keys are set
    langfuse_public_key: str | None = None
    langfuse_secret_key: SecretStr | None = None
    langfuse_host: str = "https://cloud.langfuse.com"

    # Card-to-card payment instructions shown in /buy for users who cannot buy Stars
    # (e.g. card number and holder); manual top-ups are disabled when unset.
    manual_payment_text: str | None = None

    # Telegram user ids of service admins (comma-separated): kill switch and reports
    admin_tg_ids: str = ""

    @property
    def admins(self) -> frozenset[int]:
        return frozenset(int(x) for x in self.admin_tg_ids.replace(" ", "").split(",") if x)

    # Public HTTPS URL of the mini app (in development: the cloudflared tunnel)
    miniapp_url: str | None = None

    # How the server process receives Telegram updates
    update_mode: Literal["polling", "webhook"] = "webhook"
    # Error reporting; disabled when unset
    sentry_dsn: str | None = None

    # Public HTTPS origin Telegram sends webhooks to, e.g. https://obot.example.com
    public_base_url: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
