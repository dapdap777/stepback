"""Estimated cost per call.

Prices are USD per million tokens for first-party API usage and are *estimates*: they ignore
batch discounts, regional/partner pricing and promotional credits. Unknown models yield
``None`` rather than a guess. Override or extend with ``stepback.configure(pricing={...})``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .. import config


@dataclass(frozen=True)
class ModelPrice:
    input: float
    output: float
    cache_read: float | None = None  # defaults to 0.1x input
    # Optional long-context tier: above `tier_threshold` prompt tokens use the tier prices.
    tier_threshold: int | None = None
    tier_input: float | None = None
    tier_output: float | None = None
    # Fast-mode prices (usage.speed == "fast").
    fast_input: float | None = None
    fast_output: float | None = None


# Keys are model id prefixes; the longest matching prefix wins, so dated snapshot ids
# (e.g. "claude-sonnet-4-5-20250929") resolve to their family.
ANTHROPIC_PRICES: dict[str, ModelPrice] = {
    "claude-fable-5-2": ModelPrice(8.0, 40.0, 0.20, fast_input=16.0, fast_output=80.0),
    "claude-mythos-5-2": ModelPrice(8.0, 40.0, 0.20, fast_input=16.0, fast_output=80.0),
    "claude-fable-5-1": ModelPrice(10.0, 50.0, 0.25),
    "claude-mythos-5-1": ModelPrice(10.0, 50.0, 0.25),
    "claude-fable-5": ModelPrice(10.0, 50.0, 1.0),
    "claude-mythos-5": ModelPrice(10.0, 50.0, 1.0),
    "claude-opus-5-5": ModelPrice(4.0, 20.0, 0.20, fast_input=8.0, fast_output=40.0),
    "claude-opus-5": ModelPrice(5.0, 25.0, fast_input=10.0, fast_output=50.0),
    "claude-opus-4-8": ModelPrice(5.0, 25.0),
    "claude-opus-4-7": ModelPrice(5.0, 25.0),
    "claude-opus-4-6": ModelPrice(5.0, 25.0),
    "claude-opus-4-5": ModelPrice(5.0, 25.0),
    "claude-opus-4-1": ModelPrice(15.0, 75.0),
    "claude-opus-4": ModelPrice(15.0, 75.0),
    "claude-sonnet-5-5": ModelPrice(2.0, 10.0, 0.10),
    "claude-sonnet-5": ModelPrice(2.0, 10.0),
    "claude-sonnet-4-6": ModelPrice(3.0, 15.0),
    "claude-sonnet-4-5": ModelPrice(3.0, 15.0),
    "claude-sonnet-4": ModelPrice(3.0, 15.0),
    "claude-3-7-sonnet": ModelPrice(3.0, 15.0),
    "claude-haiku-5-5": ModelPrice(
        0.10, 0.50, tier_threshold=100_000, tier_input=0.50, tier_output=2.50
    ),
    "claude-haiku-4-5": ModelPrice(1.0, 5.0),
    "claude-3-5-haiku": ModelPrice(0.80, 4.0),
    "claude-3-haiku": ModelPrice(0.25, 1.25),
}

CACHE_WRITE_5M_MULTIPLIER = 1.25
CACHE_WRITE_1H_MULTIPLIER = 2.0


def _strip_provider_prefix(model: str) -> str:
    # Bedrock ids look like "anthropic.claude-..." or "us.anthropic.claude-..."; Vertex uses "@".
    model = model.split("@", 1)[0]
    if "anthropic." in model:
        model = model.split("anthropic.", 1)[1]
    return model.removesuffix("-fast")


def _from_override(data: dict[str, Any]) -> ModelPrice:
    return ModelPrice(**data)


def lookup(model: str | None) -> ModelPrice | None:
    if not model:
        return None
    overrides = config.settings.pricing
    if model in overrides:
        return _from_override(overrides[model])
    name = _strip_provider_prefix(model)
    if name in overrides:
        return _from_override(overrides[name])
    best: str | None = None
    for key in ANTHROPIC_PRICES:
        if (name == key or name.startswith(key + "-")) and (best is None or len(key) > len(best)):
            best = key
    return ANTHROPIC_PRICES[best] if best else None


def estimate_cost(model: str | None, usage: dict[str, Any] | None) -> float | None:
    """Estimated USD cost of one call from an Anthropic ``usage`` dict."""
    if not usage:
        return None
    price = lookup(model)
    if price is None:
        return None

    def n(key: str) -> int:
        value = usage.get(key)
        return int(value) if isinstance(value, (int, float)) else 0

    input_tokens = n("input_tokens")
    output_tokens = n("output_tokens")
    cache_read = n("cache_read_input_tokens")
    cache_write_total = n("cache_creation_input_tokens")
    cache_write_1h = 0
    breakdown = usage.get("cache_creation")
    if isinstance(breakdown, dict):
        cache_write_1h = int(breakdown.get("ephemeral_1h_input_tokens") or 0)
    cache_write_5m = max(cache_write_total - cache_write_1h, 0)

    in_price, out_price = price.input, price.output
    if usage.get("speed") == "fast" and price.fast_input is not None:
        in_price, out_price = price.fast_input, price.fast_output or out_price
    prompt_tokens = input_tokens + cache_read + cache_write_total
    if price.tier_threshold is not None and prompt_tokens > price.tier_threshold:
        in_price = price.tier_input or in_price
        out_price = price.tier_output or out_price
    read_price = price.cache_read if price.cache_read is not None else in_price * 0.1

    total = (
        input_tokens * in_price
        + output_tokens * out_price
        + cache_read * read_price
        + cache_write_5m * in_price * CACHE_WRITE_5M_MULTIPLIER
        + cache_write_1h * in_price * CACHE_WRITE_1H_MULTIPLIER
    )
    return round(total / 1_000_000, 8)
