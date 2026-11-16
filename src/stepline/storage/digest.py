"""Small derived fields stored next to each step at write time.

Step lists in the UI and CLI must stay fast for runs with thousands of steps. A step's
``request`` holds the whole conversation so far, so step N carries O(N) messages, and parsing
every request to build a list would be quadratic. Instead, short previews and content hashes are
computed once when a step is written, and list queries read only these columns.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

PREVIEW_CHARS = 280
TOOL_INPUT_PREVIEW_CHARS = 120


def clip(text: str, limit: int = PREVIEW_CHARS) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def hash_json(value: Any) -> str | None:
    """Stable short hash of JSON data (key order independent). None stays None."""
    if value is None:
        return None
    canonical = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def content_text(content: Any) -> str:
    """Plain text of a message ``content`` (a string or a list of blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                btype = block.get("type")
                if btype == "text":
                    parts.append(str(block.get("text", "")))
                elif btype == "tool_result":
                    parts.append(content_text(block.get("content")))
                elif btype in ("image", "document"):
                    parts.append(f"[{btype}]")
        return " ".join(p for p in parts if p)
    return "" if content is None else str(content)


def input_preview(request: dict[str, Any] | None) -> str | None:
    """What was new in this request: the latest user text, or a note about tool results."""
    messages = (request or {}).get("messages")
    if not isinstance(messages, list) or not messages:
        return None
    last = messages[-1]
    if not isinstance(last, dict) or last.get("role") != "user":
        return None
    content = last.get("content")
    if isinstance(content, list):
        results = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
        texts = [
            str(b.get("text", ""))
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        if results:
            note = f"[{len(results)} tool result{'s' if len(results) != 1 else ''}]"
            return clip(" ".join([note, *texts]))
    text = content_text(content)
    return clip(text) if text else None


def output_preview(
    response: dict[str, Any] | None, error: dict[str, Any] | None = None
) -> str | None:
    """Assistant text, else the tool calls made, else the error."""
    if response:
        blocks = response.get("content") or []
        text = " ".join(
            str(b.get("text", ""))
            for b in blocks
            if isinstance(b, dict) and b.get("type") == "text"
        ).strip()
        if text:
            return clip(text)
        calls = [
            f"{b.get('name')}({tool_input_preview(b.get('input'), 60)})"
            for b in blocks
            if isinstance(b, dict) and b.get("type") in ("tool_use", "server_tool_use")
        ]
        if calls:
            return clip("→ " + ", ".join(calls))
    if error:
        return clip(error_message(error))
    return None


def error_message(error: dict[str, Any]) -> str:
    """Prefer the API's own error message over the SDK's exception string."""
    body = error.get("body")
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        inner = body["error"]
        if inner.get("message"):
            return f"{inner.get('type', 'error')}: {inner['message']}"
    return str(error.get("message", ""))


def tool_input_preview(value: Any, limit: int = TOOL_INPUT_PREVIEW_CHARS) -> str:
    if isinstance(value, dict):
        text = ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in value.items())
    else:
        text = json.dumps(value, ensure_ascii=False)
    return clip(text, limit)


def message_count(request: dict[str, Any] | None) -> int:
    messages = (request or {}).get("messages")
    return len(messages) if isinstance(messages, list) else 0
