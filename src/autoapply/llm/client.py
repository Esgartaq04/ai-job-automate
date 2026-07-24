"""Anthropic wrapper: structured JSON out, cost accounting in, refusals surfaced.

Model tiering follows README section 8 (cost): the fast model does match scoring,
field mapping and email classification; the smart model only writes prose that a
human will read (tailored bullets, cover letters).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from ..config import get_settings

log = logging.getLogger(__name__)

# USD per million tokens (input, output). Used for the per-run cost_cents figure.
PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-fable-5": (10.00, 50.00),
}


class LLMRefusal(RuntimeError):
    """The model declined. Never retried blindly — the caller routes to manual review."""


@dataclass
class LLMResult:
    data: Any
    cost_cents: float
    model: str
    input_tokens: int = 0
    output_tokens: int = 0


def cost_cents(model: str, input_tokens: int, output_tokens: int) -> float:
    in_rate, out_rate = PRICING.get(model, (5.00, 25.00))
    usd = (input_tokens / 1_000_000) * in_rate + (output_tokens / 1_000_000) * out_rate
    return usd * 100.0


class LLMClient:
    """Thin wrapper. Tests inject a stub with the same `complete_json` signature."""

    def __init__(self, client: Any | None = None):
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    def complete_json(
        self,
        *,
        model: str,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        max_tokens: int = 8000,
        cache_system: bool = True,
    ) -> LLMResult:
        """Ask for a JSON object matching `schema`. Structured outputs guarantee shape.

        The system prompt is cached: it's byte-identical across every job in a run,
        so after the first call it bills at cache-read rates.
        """
        system_blocks: Any = [{"type": "text", "text": system}]
        if cache_system:
            system_blocks[0]["cache_control"] = {"type": "ephemeral"}

        response = self.client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_blocks,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )

        if getattr(response, "stop_reason", None) == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise LLMRefusal(f"model refused (category={category})")

        text = next((b.text for b in response.content if b.type == "text"), "")
        usage = response.usage
        in_tok = getattr(usage, "input_tokens", 0) or 0
        out_tok = getattr(usage, "output_tokens", 0) or 0
        # Cached reads are billed at ~0.1x; count them at read rate rather than free.
        cached = getattr(usage, "cache_read_input_tokens", 0) or 0
        billed_in = in_tok + int(cached * 0.1)

        return LLMResult(
            data=json.loads(text),
            cost_cents=cost_cents(model, billed_in, out_tok),
            model=model,
            input_tokens=in_tok,
            output_tokens=out_tok,
        )


@lru_cache
def get_llm() -> LLMClient:
    return LLMClient()


def fast_model() -> str:
    return get_settings().model_fast


def smart_model() -> str:
    return get_settings().model_smart
