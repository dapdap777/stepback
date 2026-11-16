"""Small helpers shared across the package."""

from __future__ import annotations

import json
import logging
import secrets
import time
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger("stepline")
logger.addHandler(logging.NullHandler())

_warned: set[str] = set()


def new_id(prefix: str) -> str:
    """Short, sortable-enough random id like ``run_3f9a1c0b7e2d``."""
    return f"{prefix}_{secrets.token_hex(6)}"


def now() -> float:
    return time.time()


def warn_once(key: str, message: str, exc: BaseException | None = None) -> None:
    """Log an internal failure: a warning the first time per ``key``, debug afterwards."""
    if key in _warned:
        logger.debug(message, exc_info=exc)
        return
    _warned.add(key)
    logger.warning(
        "stepline: %s (recording continues; your app is unaffected)", message, exc_info=exc
    )


def snapshot_containers(value: Any) -> Any:
    """Copy dicts/lists/tuples recursively, keeping leaves as-is.

    This runs on the caller's thread and must be cheap. It protects the recorded request
    against the very common pattern of appending to ``messages`` after the call returns.
    Leaves (strings, numbers, SDK model objects) are immutable in practice, so they are shared
    and only converted to JSON later, on the background writer thread.
    """
    if isinstance(value, dict):
        return {k: snapshot_containers(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [snapshot_containers(v) for v in value]
    return value


def to_jsonable(value: Any, _depth: int = 0) -> Any:
    """Best-effort conversion of SDK objects (pydantic models etc.) into plain JSON data."""
    if _depth > 64:
        return repr(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(k): to_jsonable(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_jsonable(v, _depth + 1) for v in value]
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            try:
                dumped = model_dump(mode="json", exclude_none=True, warnings=False)
            except TypeError:  # older pydantic without the `warnings` flag
                dumped = model_dump(mode="json", exclude_none=True)
            return to_jsonable(dumped, _depth + 1)
        except Exception:
            pass
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return to_jsonable(to_dict(), _depth + 1)
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        return {
            k: to_jsonable(v, _depth + 1) for k, v in vars(value).items() if not k.startswith("_")
        }
    return repr(value)


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=repr)


def loads(value: str | None, default: Any = None) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default
