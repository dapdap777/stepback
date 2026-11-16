"""Estimated cost per call.

Prices are USD per million tokens for first-party API usage and are *estimates*: they ignore
batch discounts, regional/partner pricing and promotional credits. Unknown models yield
``None`` rather than a guess.

Override or extend the table with ``.stepline/prices.toml`` (or ``$STEPLINE_PRICES``)::

    [models."claude-opus-5-5"]      # override some fields of a built-in model
    input = 3.6

    [models."my-org/finetune-v2"]   # add a model: input and output are required
    input = 1.5
    output = 6.0
    cache_read = 0.15

or programmatically with ``stepline.configure(pricing={...})``, which takes precedence.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .. import config
from .._util import warn_once

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib


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


PRICES_FILE_NAME = "prices.toml"
_PRICE_FIELDS = set(ModelPrice.__dataclass_fields__)


class PriceFileError(ValueError):
    pass


def prices_path() -> Path:
    """``$STEPLINE_PRICES`` if set, else ``prices.toml`` next to the trace database."""
    env = os.environ.get("STEPLINE_PRICES")
    if env:
        return Path(env).expanduser()
    return config.resolve_db_path().parent / PRICES_FILE_NAME


def _merge_entry(base: ModelPrice | None, key: str, data: Any, origin: str) -> ModelPrice:
    if not isinstance(data, dict):
        raise PriceFileError(f"{origin}: entry for {key!r} must be a table of prices")
    unknown = set(data) - _PRICE_FIELDS
    if unknown:
        raise PriceFileError(
            f"{origin}: unknown field(s) {sorted(unknown)} for {key!r}; "
            f"allowed: {sorted(_PRICE_FIELDS)}"
        )
    for field_name, value in data.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            raise PriceFileError(
                f"{origin}: {key}.{field_name} must be a non-negative number, got {value!r}"
            )
    if base is None:
        missing = {"input", "output"} - set(data)
        if missing:
            raise PriceFileError(
                f"{origin}: new model {key!r} needs {' and '.join(sorted(missing))} prices"
            )
        return ModelPrice(**data)
    return replace(base, **data)


def parse_price_table(raw: dict[str, Any], origin: str) -> dict[str, dict[str, Any]]:
    """Validate the ``[models."<id>"]`` tables of a prices.toml document."""
    unknown = set(raw) - {"models"}
    if unknown:
        raise PriceFileError(f"{origin}: unknown top-level key(s) {sorted(unknown)}")
    models = raw.get("models", {})
    if not isinstance(models, dict):
        raise PriceFileError(f"{origin}: [models] must be a table")
    for key, data in models.items():
        _merge_entry(ANTHROPIC_PRICES.get(key), key, data, origin)  # validation only
    return {str(k): dict(v) for k, v in models.items()}


_file_cache: tuple[Path, int, int, dict[str, dict[str, Any]]] | None = None


def load_price_file(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Read prices.toml, cached until the file changes. Invalid files are ignored (warned)."""
    global _file_cache
    path = path or prices_path()
    try:
        stat = path.stat()
    except OSError:
        return {}
    if _file_cache and _file_cache[:3] == (path, stat.st_mtime_ns, stat.st_size):
        return _file_cache[3]
    try:
        with path.open("rb") as fh:
            entries = parse_price_table(tomllib.load(fh), str(path))
    except (OSError, ValueError) as exc:  # TOMLDecodeError is a ValueError
        warn_once(f"prices-{path}", f"ignoring invalid price file {path}: {exc}")
        entries = {}
    _file_cache = (path, stat.st_mtime_ns, stat.st_size, entries)
    return entries


def effective_prices() -> dict[str, tuple[ModelPrice, str]]:
    """The merged price table: model key -> (price, source).

    Precedence (later wins): built-in table < prices.toml < ``configure(pricing=...)``.
    Partial entries for a known model only override the fields they set.
    """
    table: dict[str, tuple[ModelPrice, str]] = {
        key: (price, "built-in") for key, price in ANTHROPIC_PRICES.items()
    }
    layers = (
        (load_price_file(), str(prices_path())),
        (config.settings.pricing, "configure()"),
    )
    for entries, source in layers:
        for key, data in entries.items():
            base = table.get(key, (None, ""))[0]
            try:
                table[key] = (_merge_entry(base, key, data, source), source)
            except PriceFileError as exc:
                warn_once(f"price-{source}-{key}", str(exc))
    return table


def lookup(model: str | None) -> ModelPrice | None:
    """Price for ``model``: exact id first, then the longest matching family prefix."""
    if not model:
        return None
    table = effective_prices()
    if model in table:
        return table[model][0]
    name = _strip_provider_prefix(model)
    best: str | None = None
    for key in table:
        if (name == key or name.startswith(key + "-")) and (best is None or len(key) > len(best)):
            best = key
    return table[best][0] if best else None


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
