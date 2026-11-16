"""Stepline: a time-travel debugger for AI agents.

Two lines to start recording::

    import stepline
    client = stepline.wrap(Anthropic())

Then run ``stepline ui`` to inspect every step and replay from any of them.
"""

from __future__ import annotations

from .config import configure
from .recorder import register_adapter, run, wrap
from .recorder.recorder import get_recorder

__version__ = "0.1.0"


def flush(timeout: float | None = None) -> bool:
    """Block until all recorded calls are written to disk. Returns False on timeout.

    Not normally needed: pending records are flushed automatically at interpreter exit.
    """
    return get_recorder().flush(timeout)


__all__ = ["__version__", "configure", "flush", "register_adapter", "run", "wrap"]
