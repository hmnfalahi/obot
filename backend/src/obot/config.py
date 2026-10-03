from functools import lru_cache

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
    # Public HTTPS origin Telegram sends webhooks to, e.g. https://obot.example.com
    public_base_url: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
