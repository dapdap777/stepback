"""Runtime configuration.

Everything is resolved lazily so that importing :mod:`stepline` has no side effects
(no directories are created and no threads are started until the first recorded call).
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_DIR_NAME = ".stepline"
DB_FILE_NAME = "traces.db"

_TRUTHY = {"1", "true", "yes", "on"}


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


@dataclass
class Settings:
    """Process-wide settings. Mutate via :func:`stepline.configure`."""

    db_path: Path | None = None
    enabled: bool = field(default_factory=lambda: not _env_flag("STEPLINE_DISABLED"))
    default_run_name: str | None = None
    # Extra/override pricing: model id -> {"input": $/MTok, "output": $/MTok, ...}
    pricing: dict[str, dict[str, float]] = field(default_factory=dict)
    # Seconds to wait for pending writes at interpreter exit.
    flush_timeout: float = 5.0


_lock = threading.Lock()
settings = Settings()


def resolve_db_path(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Where traces live.

    Precedence: explicit arg > configure(db_path) > $STEPLINE_DB > $STEPLINE_DIR > ./.stepline
    """
    if explicit is not None:
        return Path(explicit).expanduser()
    if settings.db_path is not None:
        return settings.db_path
    env_db = os.environ.get("STEPLINE_DB")
    if env_db:
        return Path(env_db).expanduser()
    env_dir = os.environ.get("STEPLINE_DIR")
    base = Path(env_dir).expanduser() if env_dir else Path.cwd() / DEFAULT_DIR_NAME
    return base / DB_FILE_NAME


def configure(
    *,
    db_path: str | os.PathLike[str] | None = None,
    enabled: bool | None = None,
    run_name: str | None = None,
    pricing: dict[str, dict[str, float]] | None = None,
    flush_timeout: float | None = None,
) -> None:
    """Adjust Stepline settings. Set ``db_path`` before the first recorded call."""
    with _lock:
        if db_path is not None:
            settings.db_path = Path(db_path).expanduser()
        if enabled is not None:
            settings.enabled = enabled
        if run_name is not None:
            settings.default_run_name = run_name
        if pricing is not None:
            settings.pricing.update(pricing)
        if flush_timeout is not None:
            settings.flush_timeout = flush_timeout


def snapshot() -> dict[str, Any]:
    return {
        "db_path": str(resolve_db_path()),
        "enabled": settings.enabled,
        "default_run_name": settings.default_run_name,
    }
