"""A tiny fake of the Anthropic Messages API, served through httpx2.MockTransport.

The real SDK builds requests and parses responses, so tests exercise realistic object shapes
without any network access.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Callable
from types import ModuleType
from typing import Any

from anthropic import (
    Anthropic,
    AsyncAnthropic,
    DefaultAsyncHttpxClient,
    DefaultHttpxClient,
)

# anthropic>=1.0 is built on httpx2; 0.x on httpx. Use whichever the installed SDK uses.
httpx2: ModuleType = importlib.import_module(
    DefaultHttpxClient.__mro__[1].__module__.split(".", 1)[0]
)

FAKE_API_KEY = "sk-ant-api03-FAKEKEYFORTESTS0123456789abcdef"
MODEL = "claude-opus-5-5"

Responder = Callable[[dict[str, Any]], dict[str, Any] | list[tuple[str, dict[str, Any]]]]


def text_message(text: str, *, input_tokens: int = 12, output_tokens: int = 7) -> dict[str, Any]:
    return {
        "id": "msg_text",
        "type": "message",
        "role": "assistant",
        "model": MODEL,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


def tool_use_message(
    tool_id: str, name: str, tool_input: dict[str, Any], text: str = "Let me check."
) -> dict[str, Any]:
    return {
        "id": f"msg_{tool_id}",
        "type": "message",
        "role": "assistant",
        "model": MODEL,
        "content": [
            {"type": "text", "text": text},
            {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input},
        ],
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": {"input_tokens": 20, "output_tokens": 15},
    }


def stream_events(
    text_chunks: list[str], *, tool: tuple[str, str, str] | None = None
) -> list[tuple[str, dict[str, Any]]]:
    """SSE events for a streamed message: text chunks then (optionally) a tool_use block.

    ``tool`` is ``(id, name, input_json_string)``.
    """
    events: list[tuple[str, dict[str, Any]]] = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_stream",
                    "type": "message",
                    "role": "assistant",
                    "model": MODEL,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 30, "output_tokens": 1},
                },
            },
        ),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
    ]
    for chunk in text_chunks:
        events.append(
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": chunk},
                },
            )
        )
    events.append(("content_block_stop", {"type": "content_block_stop", "index": 0}))
    stop_reason = "end_turn"
    if tool is not None:
        tool_id, name, input_json = tool
        stop_reason = "tool_use"
        events.append(
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {"type": "tool_use", "id": tool_id, "name": name, "input": {}},
                },
            )
        )
        half = len(input_json) // 2
        for part in (input_json[:half], input_json[half:]):
            events.append(
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 1,
                        "delta": {"type": "input_json_delta", "partial_json": part},
                    },
                )
            )
        events.append(("content_block_stop", {"type": "content_block_stop", "index": 1}))
    events.append(
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                "usage": {"output_tokens": 42},
            },
        )
    )
    events.append(("message_stop", {"type": "message_stop"}))
    return events


def _sse(events: list[tuple[str, dict[str, Any]]]) -> bytes:
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events).encode()


class FakeAPI:
    """Collects requests and answers them from a queue of canned responses."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.request_headers: list[dict[str, str]] = []
        self._queue: list[Any] = []

    def push(self, *responses: Any) -> FakeAPI:
        """Queue responses: a message dict, a list of SSE events, or an (status, body) tuple."""
        self._queue.extend(responses)
        return self

    def handler(self, request: Any) -> Any:
        body = json.loads(request.content or b"{}")
        self.requests.append(body)
        self.request_headers.append(dict(request.headers))
        if not self._queue:
            return httpx2.Response(
                500,
                json={
                    "type": "error",
                    "error": {"type": "api_error", "message": "no canned response"},
                },
            )
        item = self._queue.pop(0)
        if isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], int):
            status, payload = item
            return httpx2.Response(status, json=payload)
        model = body.get("model")
        if isinstance(item, dict) and item.get("type") == "message" and model:
            item = {**item, "model": model}  # echo the requested model, like the real API
        if isinstance(item, list):
            if model and item and item[0][0] == "message_start":
                start = dict(item[0][1])
                start["message"] = {**start["message"], "model": model}
                item = [(item[0][0], start), *item[1:]]
            return httpx2.Response(
                200, content=_sse(item), headers={"content-type": "text/event-stream"}
            )
        return httpx2.Response(200, json=item)

    def sync_client(self, **kwargs: Any) -> Anthropic:
        return Anthropic(
            api_key=FAKE_API_KEY,
            max_retries=0,
            http_client=DefaultHttpxClient(transport=httpx2.MockTransport(self.handler)),
            **kwargs,
        )

    def async_client(self, **kwargs: Any) -> AsyncAnthropic:
        async def handler(request: Any) -> Any:
            # MockTransport needs the body read before handing it to the sync handler.
            await request.aread()
            return self.handler(request)

        return AsyncAnthropic(
            api_key=FAKE_API_KEY,
            max_retries=0,
            http_client=DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
            **kwargs,
        )


def error_body(message: str = "boom", etype: str = "invalid_request_error") -> dict[str, Any]:
    return {"type": "error", "error": {"type": etype, "message": message}}
