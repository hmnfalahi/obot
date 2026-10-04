"""Bring your own key: models served from the owner's account at a provider."""

import enum
import re
from urllib.parse import quote

import httpx2
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI, DefaultAsyncHttpxClient
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.providers.openrouter import OpenRouterProvider
from pydantic_ai.settings import ModelSettings

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENAI_BASE_URL = "https://api.openai.com/v1"
ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"
REQUEST_TIMEOUT_SECONDS = 60
CHECK_TIMEOUT_SECONDS = 15
MAX_RETRIES = 2

MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}")


class Provider(enum.StrEnum):
    OPENROUTER = "openrouter"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"


class Failure(enum.StrEnum):
    """Why the owner's key could not be used; shown to them."""

    AUTH = "auth"  # invalid or revoked key
    QUOTA = "quota"  # out of money or rate-limited
    MODEL = "model"  # unknown model, or no access to it
    PROVIDER = "provider"  # anything else: outage, timeout, bad request


class KeyCheckFailed(Exception):
    def __init__(self, failure: Failure) -> None:
        super().__init__(failure.value)
        self.failure = failure


def valid_model_name(name: str) -> bool:
    return MODEL_NAME.fullmatch(name) is not None


def _failure_for_status(status: int) -> Failure:
    if status in (401, 403):
        return Failure.AUTH
    if status in (402, 429):
        return Failure.QUOTA
    if status == 404:
        return Failure.MODEL
    return Failure.PROVIDER


def failure_of(exc: BaseException) -> Failure | None:
    """Classify an exception from a model call; None when the provider is not at fault."""
    if isinstance(exc, ModelHTTPError):
        return _failure_for_status(exc.status_code)
    if isinstance(exc, ModelAPIError):
        return Failure.PROVIDER
    return None


class Byok:
    """Builds models for owners' keys; one HTTP connection pool serves every key."""

    def __init__(self, max_output_tokens: int, http: httpx2.AsyncClient | None = None) -> None:
        # The SDKs take an httpx2 client; they do not close a client they were given.
        self._http = http or DefaultAsyncHttpxClient(timeout=REQUEST_TIMEOUT_SECONDS)
        self._settings = ModelSettings(max_tokens=max_output_tokens)

    def model(self, provider: Provider, name: str, api_key: str) -> Model:
        options = {
            "api_key": api_key,
            "http_client": self._http,
            "timeout": REQUEST_TIMEOUT_SECONDS,
            "max_retries": MAX_RETRIES,
        }
        match provider:
            case Provider.OPENROUTER:
                client = AsyncOpenAI(
                    base_url=OPENROUTER_BASE_URL, default_headers={"X-Title": "Obot"}, **options
                )
                return OpenRouterModel(
                    name, provider=OpenRouterProvider(openai_client=client), settings=self._settings
                )
            case Provider.OPENAI:
                client = AsyncOpenAI(**options)
                return OpenAIChatModel(
                    name, provider=OpenAIProvider(openai_client=client), settings=self._settings
                )
            case Provider.ANTHROPIC:
                client = AsyncAnthropic(**options)
                return AnthropicModel(
                    name,
                    provider=AnthropicProvider(anthropic_client=client),
                    settings=self._settings,
                )

    async def check(self, provider: Provider, name: str, api_key: str) -> None:
        """Raise KeyCheckFailed unless the key works and can use the model.

        Uses the providers' free metadata endpoints, so checking costs the owner nothing.
        """
        try:
            match provider:
                case Provider.OPENROUTER:
                    await self._get(f"{OPENROUTER_BASE_URL}/key", _bearer(api_key))
                    models = await self._get(f"{OPENROUTER_BASE_URL}/models", {})
                    if name not in {m.get("id") for m in models.json().get("data", [])}:
                        raise KeyCheckFailed(Failure.MODEL)
                case Provider.OPENAI:
                    await self._get(
                        f"{OPENAI_BASE_URL}/models/{quote(name, safe='')}", _bearer(api_key)
                    )
                case Provider.ANTHROPIC:
                    await self._get(
                        f"{ANTHROPIC_BASE_URL}/models/{quote(name, safe='')}",
                        {"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION},
                    )
        except httpx2.HTTPError as e:
            raise KeyCheckFailed(Failure.PROVIDER) from e
        except ValueError as e:  # not JSON
            raise KeyCheckFailed(Failure.PROVIDER) from e

    async def _get(self, url: str, headers: dict[str, str]) -> httpx2.Response:
        response = await self._http.get(url, headers=headers, timeout=CHECK_TIMEOUT_SECONDS)
        if response.status_code != 200:
            raise KeyCheckFailed(_failure_for_status(response.status_code))
        return response


def _bearer(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}
