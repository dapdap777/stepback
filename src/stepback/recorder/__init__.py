"""Recording: SDK adapters, the background writer, and run scopes."""

from __future__ import annotations

from typing import TypeVar

from .._util import warn_once
from .anthropic_adapter import AnthropicAdapter
from .base import CallHandle, NormalizedCall, ProviderAdapter
from .recorder import Recorder, get_recorder, run

ClientT = TypeVar("ClientT")

#: Registered adapters, tried in order. Third-party adapters can be appended.
ADAPTERS: list[ProviderAdapter] = [AnthropicAdapter()]


def register_adapter(adapter: ProviderAdapter) -> None:
    """Add support for another SDK. Adapters registered later take precedence."""
    ADAPTERS.insert(0, adapter)


def wrap(client: ClientT) -> ClientT:
    """Record every LLM call made through ``client``. Returns the same client object.

    Never raises: if the client isn't recognized, a warning is logged and the client is
    returned untouched.
    """
    try:
        recorder = get_recorder()
        for adapter in ADAPTERS:
            if adapter.matches(client):
                return adapter.install(client, recorder)
        warn_once(
            f"unsupported-{type(client).__name__}",
            f"stepback.wrap() does not support {type(client).__module__}.{type(client).__name__}",
        )
    except Exception as exc:
        warn_once("wrap", "stepback.wrap() failed", exc)
    return client


__all__ = [
    "ADAPTERS",
    "AnthropicAdapter",
    "CallHandle",
    "NormalizedCall",
    "ProviderAdapter",
    "Recorder",
    "get_recorder",
    "register_adapter",
    "run",
    "wrap",
]
