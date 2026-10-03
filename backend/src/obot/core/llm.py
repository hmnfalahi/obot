import os

from openai import AsyncOpenAI
from pydantic_ai.models import Model
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.providers.openrouter import OpenRouterProvider
from pydantic_ai.settings import ModelSettings

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# A chat reply should not hang for minutes; failed requests are retried.
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 2


class ModelRegistry:
    """Maps a bot's model tier (e.g. "cheap", "strong") to a configured model."""

    def __init__(
        self, api_key: str, models_by_tier: dict[str, str], max_output_tokens: int
    ) -> None:
        client = AsyncOpenAI(
            base_url=OPENROUTER_BASE_URL,
            api_key=api_key,
            timeout=REQUEST_TIMEOUT_SECONDS,
            max_retries=MAX_RETRIES,
            default_headers={"X-Title": "Obot"},
        )
        self.client = client
        self._provider = OpenRouterProvider(openai_client=client)
        self._names = models_by_tier
        # Without max_tokens OpenRouter reserves the model's full output window
        # against the account balance; it also caps the cost of a single reply.
        self._settings = ModelSettings(max_tokens=max_output_tokens)
        self._cache: dict[str, Model] = {}

    def for_tier(self, tier: str) -> Model:
        name = self._names.get(tier) or self._names["cheap"]
        if name not in self._cache:
            self._cache[name] = OpenRouterModel(
                name, provider=self._provider, settings=self._settings
            )
        return self._cache[name]
