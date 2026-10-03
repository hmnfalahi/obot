import math
from decimal import Decimal
from pathlib import Path

import yaml
from pydantic import BaseModel


class ModelPrice(BaseModel):
    input_per_mtok: Decimal
    output_per_mtok: Decimal


class Plan(BaseModel):
    max_bots: int
    daily_llm_messages: int | None
    model_tiers: list[str]
    uses_credits: bool
    max_scheduled_tasks: int
    min_task_interval_minutes: int
    daily_web_searches: int
    knowledge_bytes: int


class Pricing(BaseModel):
    usd_per_credit: Decimal
    markup: Decimal
    models: dict[str, ModelPrice]
    tools: dict[str, Decimal]
    plans: dict[str, Plan]

    @classmethod
    def load(cls, path: Path) -> "Pricing":
        return cls.model_validate(yaml.safe_load(path.read_text()))

    def plan(self, name: str) -> Plan:
        return self.plans.get(name) or self.plans["free"]

    def require_models(self, model_names: list[str]) -> None:
        if missing := [m for m in model_names if m not in self.models]:
            raise ValueError(f"no price configured for models: {', '.join(missing)}")

    def cost_usd(self, model: str, tokens_in: int, tokens_out: int) -> Decimal:
        price = self.models[model]
        return (tokens_in * price.input_per_mtok + tokens_out * price.output_per_mtok) / 1_000_000

    def credits_for(self, cost_usd: Decimal) -> int:
        return math.ceil(cost_usd * self.markup / self.usd_per_credit)
