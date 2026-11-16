"""An offline stand-in for the Anthropic Messages API, for trying stepback without a key.

    from anthropic import Anthropic
    from stepback.demo.mock_api import mock_http_client

    client = Anthropic(api_key="offline", http_client=mock_http_client())

The mock plays a fixed script for the example weather agent (examples/weather_agent.py):
parallel tool calls, a failing tool, then a streamed final answer. Each turn depends only on
how many assistant turns the conversation already has (and the final answer gets shorter
if the system prompt asks for brevity, so offline replays show a difference). It speaks the
real wire format, streaming included, through the SDK's own HTTP layer. Nothing leaves your
machine and nothing is billed. Usage numbers are rough local estimates (~4 chars per token).
"""

from __future__ import annotations

import importlib
import json
import os
import time
from collections.abc import Iterator
from typing import Any

MODEL = "claude-opus-5-5"


def _http_module() -> Any:
    """anthropic>=1.0 is built on httpx2, 0.x on httpx: use whichever the SDK uses."""
    from anthropic import DefaultHttpxClient

    return importlib.import_module(DefaultHttpxClient.__mro__[1].__module__.split(".", 1)[0])


def _delay(seconds: float) -> None:
    if os.environ.get("STEPBACK_DEMO_FAST") != "1":
        time.sleep(seconds)


def _tokens(value: Any) -> int:
    return max(1, len(json.dumps(value, ensure_ascii=False)) // 4)


def _tool(tool_id: str, name: str, **arguments: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": arguments}


# One entry per assistant turn: (seconds of "thinking" before the first token, content blocks).
SCRIPT: list[tuple[float, list[dict[str, Any]]]] = [
    (
        1.1,
        [
            {"type": "thinking", "thinking": "", "signature": "mock-signature"},
            {"type": "text", "text": "I'll check the forecast for both cities first."},
            _tool("toolu_mock_01", "get_forecast", city="Lisbon", date="saturday"),
            _tool("toolu_mock_02", "get_forecast", city="Oslo", date="saturday"),
        ],
    ),
    (
        0.6,
        [
            {"type": "text", "text": "Lisbon looks warmer. Let me check air quality too."},
            _tool("toolu_mock_03", "get_air_quality", city="Lisbon"),
            _tool("toolu_mock_04", "get_air_quality", city="Oslo"),
        ],
    ),
    (
        0.8,
        [
            {
                "type": "text",
                "text": "The air quality service failed for Oslo, but I have enough to decide. "
                "Checking flight prices for Lisbon.",
            },
            _tool("toolu_mock_05", "search_flights", destination="Lisbon", date="saturday"),
        ],
    ),
    (
        0.7,
        [
            {
                "type": "text",
                "text": (
                    "**Go to Lisbon.**\n\n"
                    "| | Lisbon | Oslo |\n|---|---|---|\n"
                    "| Saturday | Sunny, 24°C | Rain, 11°C |\n"
                    "| Air quality | Good (AQI 38) | unavailable |\n"
                    "| Return flight | €142 | — |\n\n"
                    "Pack light layers and sunscreen. Evenings drop to about 16°C."
                ),
            }
        ],
    ),
]


# Scripted variant of the final answer, so a replay with an edited system prompt (e.g.
# "answer in one sentence") visibly changes the result offline.
BRIEF_ANSWER = [
    {
        "type": "text",
        "text": "Lisbon: sunny and 24°C on Saturday (Oslo: rain, 11°C), with a €142 return flight.",
    }
]
BRIEF_HINTS = ("one sentence", "brief", "terse")


def _reply(request: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    turn = sum(1 for m in request.get("messages", []) if m.get("role") == "assistant")
    delay, content = SCRIPT[min(turn, len(SCRIPT) - 1)]
    system = json.dumps(request.get("system") or "").lower()
    if turn >= len(SCRIPT) - 1 and any(hint in system for hint in BRIEF_HINTS):
        content = BRIEF_ANSWER
    stop = "tool_use" if any(b["type"] == "tool_use" for b in content) else "end_turn"
    prompt = [request.get("system"), request.get("tools"), request.get("messages")]
    message = {
        "id": f"msg_mock_{turn:02d}",
        "type": "message",
        "role": "assistant",
        "model": request.get("model") or MODEL,
        "content": content,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {"input_tokens": _tokens(prompt), "output_tokens": _tokens(content)},
    }
    return delay, message


def _sse(name: str, data: dict[str, Any]) -> bytes:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()


def _stream(message: dict[str, Any], first_token_delay: float) -> Iterator[bytes]:
    """Yield SSE events with realistic pacing, so time-to-first-token is meaningful."""
    start = dict(message, content=[], stop_reason=None)
    start["usage"] = {"input_tokens": message["usage"]["input_tokens"], "output_tokens": 1}
    yield _sse("message_start", {"type": "message_start", "message": start})
    _delay(first_token_delay)
    for index, block in enumerate(message["content"]):
        if block["type"] == "text":
            yield _sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {"type": "text", "text": ""},
                },
            )
            words = block["text"].split(" ")
            for i in range(0, len(words), 6):
                chunk = " ".join(words[i : i + 6]) + (" " if i + 6 < len(words) else "")
                yield _sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "text_delta", "text": chunk},
                    },
                )
                _delay(0.04)
        elif block["type"] == "thinking":
            yield _sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {"type": "thinking", "thinking": "", "signature": ""},
                },
            )
            yield _sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {"type": "signature_delta", "signature": block["signature"]},
                },
            )
        else:  # tool_use
            yield _sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {**block, "input": {}},
                },
            )
            yield _sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": json.dumps(block["input"]),
                    },
                },
            )
        yield _sse("content_block_stop", {"type": "content_block_stop", "index": index})
    yield _sse(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": message["stop_reason"], "stop_sequence": None},
            "usage": {"output_tokens": message["usage"]["output_tokens"]},
        },
    )
    yield _sse("message_stop", {"type": "message_stop"})


def mock_http_client() -> Any:
    """An SDK HTTP client whose transport answers locally from :data:`SCRIPT`."""
    from anthropic import DefaultHttpxClient

    http = _http_module()

    def handler(request: Any) -> Any:
        body = json.loads(request.content or b"{}")
        if request.url.path.endswith("/count_tokens"):
            prompt = [body.get("system"), body.get("tools"), body.get("messages")]
            return http.Response(200, json={"input_tokens": _tokens(prompt)})
        delay, message = _reply(body)
        if body.get("stream"):
            return http.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_stream(message, delay),
            )
        _delay(delay)
        return http.Response(200, json=message)

    return DefaultHttpxClient(transport=http.MockTransport(handler))


def mock_client() -> Any:
    """An ``anthropic.Anthropic`` client wired to the offline mock (used by ``ui --mock``)."""
    from anthropic import Anthropic

    return Anthropic(api_key="offline-mock", max_retries=0, http_client=mock_http_client())
