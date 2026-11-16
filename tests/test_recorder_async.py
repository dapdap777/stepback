from __future__ import annotations

import asyncio
from typing import Any

import anthropic
import pytest

import stepback
from fakes import MODEL, FakeAPI, error_body, stream_events, text_message, tool_use_message


async def test_async_create(fake_api: FakeAPI, recorded: Any) -> None:
    raw = fake_api.async_client()
    client = stepback.wrap(raw)
    assert client is raw and isinstance(client, anthropic.AsyncAnthropic)
    fake_api.push(text_message("async hello"))
    response = await client.messages.create(
        model=MODEL, max_tokens=10, messages=[{"role": "user", "content": "hi"}]
    )
    assert response.content[0].text == "async hello"
    (step,) = recorded()
    assert step.operation == "messages.create"
    assert step.response is not None and step.response["content"][0]["text"] == "async hello"


async def test_async_error(fake_api: FakeAPI, recorded: Any) -> None:
    client = stepback.wrap(fake_api.async_client())
    fake_api.push((429, error_body("slow down", "rate_limit_error")))
    with pytest.raises(anthropic.RateLimitError):
        await client.messages.create(
            model=MODEL, max_tokens=10, messages=[{"role": "user", "content": "hi"}]
        )
    (step,) = recorded()
    assert step.error is not None and step.error["status_code"] == 429


async def test_async_stream_create(fake_api: FakeAPI, recorded: Any) -> None:
    client = stepback.wrap(fake_api.async_client())
    fake_api.push(stream_events(["as", "ync"], tool=("toolu_a", "calc", '{"x": 1}')))
    stream = await client.messages.create(
        model=MODEL, max_tokens=10, messages=[{"role": "user", "content": "hi"}], stream=True
    )
    assert isinstance(stream, anthropic.AsyncStream)
    types = [event.type async for event in stream]
    assert types[-1] == "message_stop"
    (step,) = recorded()
    assert step.streamed and not step.incomplete
    assert step.response is not None
    assert step.response["content"][0]["text"] == "async"
    assert step.tool_calls[0]["input"] == {"x": 1}
    assert step.ttft_ms is not None


async def test_async_stream_helper(fake_api: FakeAPI, recorded: Any) -> None:
    client = stepback.wrap(fake_api.async_client())
    fake_api.push(stream_events(["he", "llo"]))
    async with client.messages.stream(
        model=MODEL, max_tokens=10, messages=[{"role": "user", "content": "hi"}]
    ) as stream:
        chunks = [text async for text in stream.text_stream]
        final = await stream.get_final_message()
    assert "".join(chunks) == "hello" and final.content[0].text == "hello"
    (step,) = recorded()
    assert step.operation == "messages.stream"
    assert step.response is not None and step.response["content"][0]["text"] == "hello"


async def test_async_runs_are_isolated_per_task(fake_api: FakeAPI, recorded: Any) -> None:
    client = stepback.wrap(fake_api.async_client())
    fake_api.push(
        tool_use_message("toolu_x", "t", {}),
        tool_use_message("toolu_y", "t", {}),
    )

    async def agent(name: str) -> str | None:
        async with stepback.run(name) as r:
            await client.messages.create(
                model=MODEL, max_tokens=10, messages=[{"role": "user", "content": name}]
            )
            return r.id

    ids = await asyncio.gather(agent("a"), agent("b"))
    steps = recorded()
    assert len(steps) == 2
    assert {s.run_id for s in steps} == set(ids)
    assert all(s.idx == 0 for s in steps)
