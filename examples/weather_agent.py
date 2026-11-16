"""A small tool-using agent, recorded with stepback.

    python examples/weather_agent.py --mock   # offline, no API key needed
    python examples/weather_agent.py          # real API (needs ANTHROPIC_API_KEY)
    stepback ui                               # then inspect the run

The agent compares weekend weather in two cities using three toy tools (canned data, so
results are reproducible). The only stepback-specific code is the import and the
`stepback.wrap(...)` call in main().
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from anthropic import Anthropic

import stepback

MODEL = "claude-opus-5-5"
MAX_TURNS = 8
SYSTEM = (
    "You are a concise travel assistant. Use the tools for every fact about weather, air "
    "quality and flights. Finish with a short recommendation."
)
QUESTION = (
    "I'm choosing between Lisbon and Oslo for this Saturday. Which has better weather, "
    "and what would a flight to the better one cost?"
)

TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_forecast",
        "description": "Weather forecast for a city on a given day.",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}, "date": {"type": "string"}},
            "required": ["city", "date"],
        },
    },
    {
        "name": "get_air_quality",
        "description": "Current air quality index for a city.",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
    {
        "name": "search_flights",
        "description": "Cheapest return flight from London to a city on a given day.",
        "input_schema": {
            "type": "object",
            "properties": {"destination": {"type": "string"}, "date": {"type": "string"}},
            "required": ["destination", "date"],
        },
    },
]
for tool in TOOLS:
    # Stream tool inputs as they're generated (we validate them before running the tool).
    tool["eager_input_streaming"] = True

FORECASTS = {"lisbon": "Sunny, 24°C, light wind", "oslo": "Rain, 11°C, gusty"}
AIR_QUALITY = {"lisbon": "AQI 38 (good)"}
FLIGHTS = {"lisbon": "€142 return (TAP, 07:05 → 09:55)", "oslo": "€188 return (SAS, 08:40 → 11:35)"}


def run_tool(name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
    """Run one tool call. Returns (result text, is_error)."""
    spec = next((t for t in TOOLS if t["name"] == name), None)
    if spec is None:
        return f"unknown tool {name!r}", True
    missing = [k for k in spec["input_schema"]["required"] if not isinstance(arguments.get(k), str)]
    if missing:
        return f"invalid input: missing or non-string {', '.join(missing)}", True

    city = str(arguments.get("city") or arguments.get("destination", "")).strip().lower()
    if name == "get_forecast":
        return FORECASTS.get(city, f"no forecast available for {city}"), city not in FORECASTS
    if name == "get_air_quality":
        if city not in AIR_QUALITY:
            return "air quality service unavailable (HTTP 503)", True
        return AIR_QUALITY[city], False
    return FLIGHTS.get(city, f"no flights found to {city}"), city not in FLIGHTS


def run_agent(client: Anthropic, question: str) -> str:
    messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
    for _ in range(MAX_TURNS):
        with client.messages.stream(
            model=MODEL, max_tokens=4096, system=SYSTEM, tools=TOOLS, messages=messages
        ) as stream:
            for text in stream.text_stream:
                print(text, end="", flush=True)
            message = stream.get_final_message()
        print()

        if message.stop_reason == "refusal":
            return "(the model declined this request)"
        if message.stop_reason == "max_tokens":
            return "(stopped: hit max_tokens before finishing)"

        # Pass the content back unchanged (it may include thinking blocks).
        messages.append({"role": "assistant", "content": message.content})
        if message.stop_reason != "tool_use":
            return "".join(b.text for b in message.content if b.type == "text")

        results = []
        for block in message.content:
            if block.type == "tool_use":
                output, is_error = run_tool(block.name, dict(block.input))
                print(f"  → {block.name}({json.dumps(block.input)}) = {output}")
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": output,
                        "is_error": is_error,
                    }
                )
        # All results for one turn go back in a single user message.
        messages.append({"role": "user", "content": results})
    return "(stopped after too many turns)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--mock", action="store_true", help="use the offline mock API (no key, no cost)"
    )
    parser.add_argument("--question", default=QUESTION)
    args = parser.parse_args(argv)

    if args.mock:
        from stepback.demo.mock_api import mock_http_client

        client = Anthropic(api_key="offline-mock", http_client=mock_http_client())
    else:
        client = Anthropic()  # reads ANTHROPIC_API_KEY

    client = stepback.wrap(client)  # ← every call through `client` is now recorded

    with stepback.run("weather-agent (mock)" if args.mock else "weather-agent"):
        run_agent(client, args.question)

    stepback.flush()
    print("\nRecorded. Inspect it with:  stepback ui   (or: stepback show)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
