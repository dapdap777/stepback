"""Scrub secrets from data before it is written to disk.

Stepline only stores request *bodies* (never headers or client configuration), so API keys
should not normally reach it at all. This module is the second line of defense: anything that
looks like a credential inside messages, tool inputs, tool results or error messages is
replaced with ``[REDACTED]``.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

# Patterns for well-known credential formats.
_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),  # Anthropic
    re.compile(r"sk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}"),  # OpenAI-style
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),  # GitHub tokens
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}\b"),  # Slack
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),  # Google API key
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-+/=]{20,}"),  # Authorization: Bearer ...
]

# Dict keys whose values are always secret, whatever they look like.
_SECRET_KEYS = re.compile(
    r"^(?:x[-_])?(?:api[-_]?key|auth(?:orization)?|access[-_]?token|secret|password|passwd"
    r"|client[-_]?secret|refresh[-_]?token|private[-_]?key|anthropic[-_]api[-_]key"
    r"|openai[-_]api[-_]key)$",
    re.IGNORECASE,
)

_extra_patterns: list[re.Pattern[str]] = []


def add_pattern(pattern: str | re.Pattern[str]) -> None:
    """Register an extra regex whose matches will be redacted."""
    _extra_patterns.append(re.compile(pattern) if isinstance(pattern, str) else pattern)


def redact_text(text: str) -> str:
    for pattern in _PATTERNS:
        text = pattern.sub(REDACTED, text)
    for pattern in _extra_patterns:
        text = pattern.sub(REDACTED, text)
    return text


def redact(value: Any) -> Any:
    """Return a copy of JSON-like ``value`` with secrets replaced."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and _SECRET_KEYS.match(key) and isinstance(item, (str, int)):
                out[key] = REDACTED
            else:
                out[key] = redact(item)
        return out
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value
