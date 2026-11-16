"""The SDK internals Stepback relies on.

If one of these fails after an anthropic upgrade, the recorder still won't break user code
(it degrades to recording without bodies), but the integration needs updating.
"""

from __future__ import annotations

from typing import Any

import anthropic
import pytest

import stepback
from fakes import MODEL, FakeAPI, stream_events
from stepback.recorder.anthropic_adapter import (
    SUPPORTED_ANTHROPIC,
    _parse_version,
    check_sdk_version,
)


def test_installed_sdk_is_in_supported_range() -> None:
    floor, cap = SUPPORTED_ANTHROPIC
    assert floor <= _parse_version(anthropic.__version__) < cap
    assert check_sdk_version()


def test_async_resources_are_named_async(fake_api: FakeAPI) -> None:
    # Async detection relies on these class names because the SDK's argument-validation
    # decorator hides coroutine functions.
    client = fake_api.async_client()
    assert type(client.messages).__name__.startswith("Async")
    assert type(client.beta.messages).__name__.startswith("Async")
    sync_client = fake_api.sync_client()
    assert not type(sync_client.messages).__name__.startswith("Async")


def test_stream_objects_expose_iterator(fake_api: FakeAPI) -> None:
    client = fake_api.sync_client()
    fake_api.push(stream_events(["x"]))
    stream = client.messages.create(
        model=MODEL, max_tokens=5, messages=[{"role": "user", "content": "x"}], stream=True
    )
    assert hasattr(stream, "_iterator")
    stream.close()


def test_message_stream_exposes_raw_stream(fake_api: FakeAPI) -> None:
    client = fake_api.sync_client()
    fake_api.push(stream_events(["x"]))
    with client.messages.stream(
        model=MODEL, max_tokens=5, messages=[{"role": "user", "content": "x"}]
    ) as stream:
        assert isinstance(stream._raw_stream, anthropic.Stream)


@pytest.mark.parametrize(
    ("version", "expected"),
    [("0.40.0", (0, 40)), ("2.8.0", (2, 8)), ("1.0.0rc1", (1, 0)), ("3", (3, 0))],
)
def test_parse_version(version: str, expected: tuple[int, int]) -> None:
    assert _parse_version(version) == expected


def test_out_of_range_version_warns_but_still_wraps(
    fake_api: FakeAPI, monkeypatch: pytest.MonkeyPatch, caplog: Any
) -> None:
    import importlib.metadata

    monkeypatch.setattr(importlib.metadata, "version", lambda name: "9.0.0")
    with caplog.at_level("WARNING", logger="stepback"):
        assert not check_sdk_version()
    client = stepback.wrap(fake_api.sync_client())
    assert getattr(client, "_stepback_wrapped", False)


def test_supported_range_is_declared_consistently() -> None:
    """pyproject.toml, SUPPORTED_ANTHROPIC and the CI matrix must agree on the range."""
    import re
    import sys
    from pathlib import Path

    if sys.version_info >= (3, 11):
        import tomllib
    else:  # pragma: no cover
        import tomli as tomllib

    root = Path(__file__).resolve().parent.parent
    pyproject = tomllib.loads((root / "pyproject.toml").read_text())
    (spec,) = pyproject["project"]["optional-dependencies"]["anthropic"]
    match = re.fullmatch(r"anthropic>=(\d+)\.(\d+),<(\d+)", spec)
    assert match, spec
    floor_major, floor_minor, cap_major = (int(g) for g in match.groups())
    assert ((floor_major, floor_minor), (cap_major, 0)) == SUPPORTED_ANTHROPIC

    ci = (root / ".github" / "workflows" / "ci.yml").read_text()
    assert f'ANTHROPIC_OLDEST: "{floor_major}.{floor_minor}.0"' in ci
