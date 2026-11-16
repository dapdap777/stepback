from __future__ import annotations

from typing import Any

import pytest

import stepback
from stepback.recorder.pricing import (
    effective_prices,
    estimate_cost,
    load_price_file,
    lookup,
    prices_path,
)
from stepback.recorder.redact import REDACTED, add_pattern, redact, redact_text


@pytest.mark.parametrize(
    ("model", "expected_input"),
    [
        ("claude-opus-5-5", 4.0),
        ("claude-opus-5", 5.0),
        ("claude-sonnet-4-5-20250929", 3.0),
        ("anthropic.claude-sonnet-5-5", 2.0),
        ("us.anthropic.claude-haiku-4-5-20251001-v1:0", 1.0),
        ("claude-opus-4-5@20251101", 5.0),
    ],
)
def test_lookup_resolves_families(model: str, expected_input: float) -> None:
    price = lookup(model)
    assert price is not None and price.input == expected_input


def test_unknown_model_has_no_cost() -> None:
    assert lookup("gpt-unknown") is None
    assert estimate_cost("gpt-unknown", {"input_tokens": 10}) is None
    assert estimate_cost("claude-opus-5-5", None) is None


def test_cost_with_cache_and_fast_mode() -> None:
    usage = {
        "input_tokens": 1_000,
        "output_tokens": 1_000,
        "cache_read_input_tokens": 10_000,
        "cache_creation_input_tokens": 3_000,
        "cache_creation": {"ephemeral_1h_input_tokens": 1_000, "ephemeral_5m_input_tokens": 2_000},
    }
    # Opus 5.5: $4 in, $20 out, $0.20 cache read, writes 1.25x (5m) / 2x (1h) input.
    expected = (1_000 * 4 + 1_000 * 20 + 10_000 * 0.20 + 2_000 * 5 + 1_000 * 8) / 1e6
    assert estimate_cost("claude-opus-5-5", usage) == pytest.approx(expected)
    fast = estimate_cost("claude-opus-5-5", {"input_tokens": 1_000_000, "speed": "fast"})
    assert fast == pytest.approx(8.0)


def test_long_context_tier() -> None:
    small = estimate_cost("claude-haiku-5-5", {"input_tokens": 100_000, "output_tokens": 0})
    big = estimate_cost("claude-haiku-5-5", {"input_tokens": 100_001, "output_tokens": 0})
    assert small == pytest.approx(0.01)
    assert big == pytest.approx(100_001 * 0.5 / 1e6)


def test_pricing_override() -> None:
    stepback.configure(pricing={"my-finetune": {"input": 1.0, "output": 2.0}})
    assert (
        estimate_cost("my-finetune", {"input_tokens": 1_000_000, "output_tokens": 1_000_000}) == 3.0
    )


@pytest.mark.parametrize(
    "secret",
    [
        "sk-ant-api03-abcdefghijklmnop_QRSTUV-123",
        "sk-proj-abcdefghijklmnopqrstuvwxyz012345",
        "AKIAABCDEFGHIJKLMNOP",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "xoxb-1234567890-abcdefghij",
        "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc",
    ],
)
def test_redact_text_patterns(secret: str) -> None:
    out = redact_text(f"before {secret} after")
    assert secret not in out
    assert REDACTED in out
    assert out.startswith("before ")


def test_redact_nested_and_secret_keys() -> None:
    data = {
        "messages": [{"role": "user", "content": "key sk-ant-api03-zzzzzzzzzzzzzzzz"}],
        "headers": {"Authorization": "whatever", "x-api-key": "plain", "accept": "json"},
        "password": "hunter2",
        "count": 3,
        "nested": [["sk-ant-admin01-yyyyyyyyyyyyyyyy"]],
    }
    out = redact(data)
    assert out["messages"][0]["content"] == f"key {REDACTED}"
    assert out["headers"] == {"Authorization": REDACTED, "x-api-key": REDACTED, "accept": "json"}
    assert out["password"] == REDACTED
    assert out["count"] == 3
    assert out["nested"] == [[REDACTED]]
    # Input is not mutated.
    assert data["password"] == "hunter2"


def test_ordinary_text_untouched() -> None:
    text = "The skeleton key task: ask-anything mode, sk-short."
    assert redact_text(text) == text


def test_custom_pattern() -> None:
    add_pattern(r"INTERNAL-[0-9]{6}")
    assert redact_text("id INTERNAL-123456") == f"id {REDACTED}"


# ---------------------------------------------------------------------- prices.toml
def _write_prices(db_path: Any, text: str) -> Any:
    path = db_path.parent / "prices.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_price_file_adds_and_overrides_models(db_path: Any) -> None:
    _write_prices(
        db_path,
        """
        [models."my-org/finetune"]
        input = 1.5
        output = 6.0

        [models."claude-opus-5-5"]
        input = 3.0
        """,
    )
    custom = lookup("my-org/finetune")
    assert custom is not None and (custom.input, custom.output) == (1.5, 6.0)
    # Partial override keeps the other built-in fields...
    opus = lookup("claude-opus-5-5")
    assert opus is not None and (opus.input, opus.output, opus.cache_read) == (3.0, 20.0, 0.20)
    # ...and applies to dated variants of the family.
    dated = lookup("claude-opus-5-5-20270101")
    assert dated is not None and dated.input == 3.0
    sources = effective_prices()
    assert sources["claude-opus-5-5"][1].endswith("prices.toml")
    assert sources["claude-haiku-5-5"][1] == "built-in"


def test_configure_beats_price_file(db_path: Any) -> None:
    _write_prices(db_path, '[models."claude-sonnet-5-5"]\ninput = 1.0\n')
    stepback.configure(pricing={"claude-sonnet-5-5": {"input": 0.5}})
    price = lookup("claude-sonnet-5-5")
    assert price is not None and price.input == 0.5 and price.output == 10.0


def test_price_file_reloads_when_changed(db_path: Any) -> None:
    path = _write_prices(db_path, '[models."m-x"]\ninput = 1\noutput = 1\n')
    assert lookup("m-x") is not None
    path.write_text('[models."m-x"]\ninput = 2\noutput = 2\n# changed size\n')
    price = lookup("m-x")
    assert price is not None and price.input == 2


def test_env_var_points_to_price_file(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "elsewhere.toml"
    path.write_text('[models."env-model"]\ninput = 7\noutput = 8\n')
    monkeypatch.setenv("STEPBACK_PRICES", str(path))
    assert prices_path() == path
    price = lookup("env-model")
    assert price is not None and price.output == 8


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('[models."new-model"]\ninput = 1\n', "needs output"),
        ('[models."claude-opus-5-5"]\ninptu = 1\n', "unknown field"),
        ('[models."claude-opus-5-5"]\ninput = -1\n', "non-negative"),
        ('[prices."x"]\ninput = 1\n', "unknown top-level"),
        ("not = [valid toml", ""),
    ],
)
def test_invalid_price_file_is_ignored_with_warning(
    db_path: Any, text: str, message: str, caplog: Any
) -> None:
    import stepback._util as util

    util._warned.clear()
    _write_prices(db_path, text)
    with caplog.at_level("WARNING", logger="stepback"):
        assert load_price_file() == {}
        price = lookup("claude-opus-5-5")
    assert price is not None and price.input == 4.0  # built-in still used
    assert "ignoring invalid price file" in caplog.text
    assert message in caplog.text
