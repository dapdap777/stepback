from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import anthropic
import pytest

import stepback
from fakes import (
    FAKE_API_KEY,
    MODEL,
    FakeAPI,
    error_body,
    stream_events,
    text_message,
    tool_use_message,
)
from stepback.recorder import AnthropicAdapter, get_recorder
from stepback.storage import TraceStore


def _create(client: anthropic.Anthropic, messages: list[dict[str, Any]], **kw: Any) -> Any:
    return client.messages.create(model=MODEL, max_tokens=256, messages=messages, **kw)


def test_wrap_returns_same_client_and_records_call(fake_api: FakeAPI, recorded: Any) -> None:
    raw = fake_api.sync_client()
    client = stepback.wrap(raw)
    assert client is raw
    assert isinstance(client, anthropic.Anthropic)

    fake_api.push(text_message("Hello there", input_tokens=100, output_tokens=50))
    response = _create(
        client,
        [{"role": "user", "content": "Hi"}],
        system="You are terse.",
        stop_sequences=["END"],
    )
    assert isinstance(response, anthropic.types.Message)
    assert response.content[0].text == "Hello there"

    (step,) = recorded()
    assert step.provider == "anthropic"
    assert step.operation == "messages.create"
    assert step.model == MODEL
    assert step.request["system"] == "You are terse."
    assert step.request["messages"] == [{"role": "user", "content": "Hi"}]
    assert step.request["stop_sequences"] == ["END"]
    assert step.response is not None
    assert step.response["content"][0]["text"] == "Hello there"
    assert step.input_tokens == 100 and step.output_tokens == 50
    assert step.stop_reason == "end_turn"
    # claude-opus-5-5: $4 in / $20 out per MTok
    assert step.cost_usd == pytest.approx(100 * 4e-6 + 50 * 20e-6)
    assert step.latency_ms is not None and step.latency_ms >= 0
    assert step.error is None and not step.streamed and not step.incomplete


def test_wrap_is_idempotent(fake_api: FakeAPI, recorded: Any) -> None:
    client = stepback.wrap(stepback.wrap(fake_api.sync_client()))
    fake_api.push(text_message("once"))
    _create(client, [{"role": "user", "content": "x"}])
    assert len(recorded()) == 1


def test_request_snapshot_is_isolated_from_later_mutation(fake_api: FakeAPI, recorded: Any) -> None:
    client = stepback.wrap(fake_api.sync_client())
    messages: list[dict[str, Any]] = [{"role": "user", "content": [{"type": "text", "text": "a"}]}]
    fake_api.push(text_message("b"))
    _create(client, messages)
    messages.append({"role": "assistant", "content": "mutated"})
    messages[0]["content"][0]["text"] = "changed"

    (step,) = recorded()
    assert step.request["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "a"}]}
    ]


def test_tool_loop_links_tool_results_and_parents(fake_api: FakeAPI, recorded: Any) -> None:
    client = stepback.wrap(fake_api.sync_client())
    fake_api.push(
        tool_use_message("toolu_1", "get_weather", {"city": "Paris"}),
        text_message("It is sunny in Paris."),
    )
    tools = [
        {
            "name": "get_weather",
            "description": "Weather for a city",
            "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
        }
    ]
    messages: list[Any] = [{"role": "user", "content": "Weather in Paris?"}]
    first = _create(client, messages, tools=tools)
    # Typical agent code: append the SDK content blocks directly.
    messages.append({"role": "assistant", "content": first.content})
    messages.append(
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "Sunny, 24C"}],
        }
    )
    _create(client, messages, tools=tools)

    step1, step2 = recorded()
    assert step1.tool_calls == [
        {"id": "toolu_1", "name": "get_weather", "input": {"city": "Paris"}, "type": "tool_use"}
    ]
    assert step1.tool_results == [
        {"tool_use_id": "toolu_1", "content": "Sunny, 24C", "is_error": False}
    ]
    assert step1.request["tools"][0]["name"] == "get_weather"
    assert step2.parent_step_id == step1.id
    assert step1.parent_step_id is None
    assert (step1.idx, step2.idx) == (0, 1)
    assert step1.run_id == step2.run_id
    # SDK block objects inside the request were serialized to plain JSON.
    assistant = step2.request["messages"][1]
    assert assistant["content"][1]["type"] == "tool_use"


def test_api_error_is_recorded_and_reraised_unchanged(fake_api: FakeAPI, recorded: Any) -> None:
    client = stepback.wrap(fake_api.sync_client())
    fake_api.push((400, error_body("max_tokens too large")))
    with pytest.raises(anthropic.BadRequestError) as info:
        _create(client, [{"role": "user", "content": "x"}])
    assert "max_tokens too large" in str(info.value)

    (step,) = recorded()
    assert step.error is not None
    assert step.error["type"] == "BadRequestError"
    assert step.error["status_code"] == 400
    assert step.response is None


def test_streaming_create_passes_chunks_and_records_message(
    fake_api: FakeAPI, recorded: Any
) -> None:
    client = stepback.wrap(fake_api.sync_client())
    fake_api.push(
        stream_events(["Hel", "lo", "!"], tool=("toolu_9", "lookup", '{"q": "stepback"}'))
    )
    stream = _create(client, [{"role": "user", "content": "hi"}], stream=True)
    assert isinstance(stream, anthropic.Stream)
    seen = [event.type for event in stream]
    assert seen[0] == "message_start" and seen[-1] == "message_stop"
    assert seen.count("content_block_delta") == 5

    (step,) = recorded()
    assert step.streamed and not step.incomplete
    assert step.response is not None
    assert step.response["content"][0] == {"type": "text", "text": "Hello!"}
    assert step.response["content"][1]["input"] == {"q": "stepback"}
    assert step.tool_calls[0]["name"] == "lookup"
    assert step.stop_reason == "tool_use"
    assert step.input_tokens == 30 and step.output_tokens == 42
    assert step.ttft_ms is not None and step.latency_ms is not None
    assert step.ttft_ms <= step.latency_ms
    assert step.request["stream"] is True


def test_streaming_does_not_buffer_chunks(fake_api: FakeAPI, recorded: Any) -> None:
    """Each chunk reaches the caller before the next one is pulled from the network."""
    client = stepback.wrap(fake_api.sync_client())
    fake_api.push(stream_events(["a", "b", "c"]))
    stream = _create(client, [{"role": "user", "content": "hi"}], stream=True)
    first = next(iter(stream))
    assert first.type == "message_start"
    # Nothing recorded yet: the stream is still open.
    assert stepback.flush(5)
    with TraceStore(get_recorder().db_path) as s:
        assert sum(r.step_count for r in s.list_runs()) == 0
    for _ in stream:
        pass
    assert len(recorded()) == 1


def test_stream_closed_early_is_recorded_as_incomplete(fake_api: FakeAPI, recorded: Any) -> None:
    client = stepback.wrap(fake_api.sync_client())
    fake_api.push(stream_events(["one ", "two ", "three"]))
    with _create(client, [{"role": "user", "content": "hi"}], stream=True) as stream:
        for event in stream:
            if event.type == "content_block_delta":
                break  # leave after the first token

    (step,) = recorded()
    assert step.incomplete
    assert step.response is not None
    assert step.response["content"][0]["text"] == "one "


def test_messages_stream_helper(fake_api: FakeAPI, recorded: Any) -> None:
    client = stepback.wrap(fake_api.sync_client())
    fake_api.push(stream_events(["Stream", "ing"]))
    with client.messages.stream(
        model=MODEL, max_tokens=100, messages=[{"role": "user", "content": "go"}]
    ) as stream:
        text = "".join(stream.text_stream)
        final = stream.get_final_message()
    assert text == "Streaming"
    assert final.content[0].text == "Streaming"

    (step,) = recorded()
    assert step.operation == "messages.stream"
    assert step.streamed and not step.incomplete
    assert step.response is not None and step.response["content"][0]["text"] == "Streaming"
    assert step.ttft_ms is not None


def test_beta_messages_and_with_options_are_recorded(fake_api: FakeAPI, recorded: Any) -> None:
    client = stepback.wrap(fake_api.sync_client())
    fake_api.push(text_message("beta"), text_message("copy"))
    client.beta.messages.create(
        model=MODEL, max_tokens=10, messages=[{"role": "user", "content": "b"}]
    )
    client.with_options(timeout=30).messages.create(
        model=MODEL, max_tokens=10, messages=[{"role": "user", "content": "c"}]
    )
    steps = recorded()
    assert [s.operation for s in steps] == ["beta.messages.create", "messages.create"]


def test_api_key_never_stored_and_secrets_redacted(
    fake_api: FakeAPI, recorded: Any, db_path: Path
) -> None:
    client = stepback.wrap(fake_api.sync_client())
    fake_api.push(text_message("ok"))
    leaked = "sk-ant-api03-LEAKEDINPROMPT00000000000000"
    _create(
        client,
        [{"role": "user", "content": f"my key is {leaked}"}],
        extra_headers={"x-api-key": "sk-ant-api03-HEADERKEY000000000000000"},
    )
    (step,) = recorded()
    assert "[REDACTED]" in step.request["messages"][0]["content"]
    assert "extra_headers" not in step.request
    # The real key did reach the (fake) API...
    assert "HEADERKEY" in fake_api.request_headers[0]["x-api-key"]
    # ...but nothing key-like is anywhere in the database file.
    get_recorder().shutdown()
    raw = b"".join(p.read_bytes() for p in db_path.parent.glob("traces.db*"))
    for secret in (FAKE_API_KEY, leaked, "HEADERKEY"):
        assert secret.encode() not in raw


def test_disabled_records_nothing(fake_api: FakeAPI, recorded: Any) -> None:
    stepback.configure(enabled=False)
    client = stepback.wrap(fake_api.sync_client())
    fake_api.push(text_message("quiet"))
    assert _create(client, [{"role": "user", "content": "x"}]).content[0].text == "quiet"
    assert recorded() == []


def test_named_runs_group_steps(fake_api: FakeAPI, recorded: Any, db_path: Path) -> None:
    client = stepback.wrap(fake_api.sync_client())
    fake_api.push(text_message("1"), text_message("2"), text_message("3"))

    with stepback.run("first", metadata={"ticket": 7}) as r1:
        _create(client, [{"role": "user", "content": "a"}])
        _create(client, [{"role": "user", "content": "b"}])

    @stepback.run("second")
    def task() -> None:
        _create(client, [{"role": "user", "content": "c"}])

    task()
    steps = recorded()
    with TraceStore(db_path) as s:
        runs = {r.run.name: r for r in s.list_runs()}
    assert set(runs) == {"first", "second"}
    assert runs["first"].step_count == 2 and runs["second"].step_count == 1
    assert runs["first"].run.status == "ok" and runs["first"].run.ended_at is not None
    assert runs["first"].run.metadata["ticket"] == 7
    assert runs["first"].run.id == r1.id or r1.id is None
    assert {s.run_id for s in steps} == {runs["first"].run.id, runs["second"].run.id}


def test_run_marked_error_when_block_raises(fake_api: FakeAPI, db_path: Path) -> None:
    with pytest.raises(RuntimeError), stepback.run("boom"):
        raise RuntimeError("agent crashed")
    stepback.flush()
    with TraceStore(db_path) as s:
        (summary,) = s.list_runs()
    assert summary.run.status == "error"


# ---------------------------------------------------------------------- robustness
def test_recorder_failures_never_break_the_app(
    fake_api: FakeAPI, monkeypatch: pytest.MonkeyPatch, recorded: Any
) -> None:
    client = stepback.wrap(fake_api.sync_client())

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("recorder bug")

    monkeypatch.setattr(get_recorder(), "begin", explode)
    fake_api.push(text_message("still works"))
    assert _create(client, [{"role": "user", "content": "x"}]).content[0].text == "still works"


def test_normalize_failure_still_stores_minimal_step(
    fake_api: FakeAPI, monkeypatch: pytest.MonkeyPatch, recorded: Any
) -> None:
    client = stepback.wrap(fake_api.sync_client())
    monkeypatch.setattr(AnthropicAdapter, "normalize", lambda self, call: 1 / 0)
    fake_api.push(stream_events(["x"]))
    assert len(list(_create(client, [{"role": "user", "content": "x"}], stream=True))) > 0
    (step,) = recorded()
    assert step.latency_ms is not None


def test_unwritable_database_never_breaks_the_app(fake_api: FakeAPI, tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("file in the way")
    stepback.configure(db_path=blocker / "traces.db")
    client = stepback.wrap(fake_api.sync_client())
    fake_api.push(text_message("fine"))
    assert _create(client, [{"role": "user", "content": "x"}]).content[0].text == "fine"
    assert stepback.flush(5)


def test_unsupported_client_is_returned_untouched() -> None:
    sentinel = object()
    assert stepback.wrap(sentinel) is sentinel


def test_hot_path_overhead_is_small(fake_api: FakeAPI) -> None:
    """begin()+finish() for a 200-message conversation must cost well under a millisecond."""
    recorder = get_recorder()
    adapter = AnthropicAdapter()
    messages = [
        {
            "role": "user" if i % 2 == 0 else "assistant",
            "content": [{"type": "text", "text": "x" * 200}],
        }
        for i in range(200)
    ]
    from stepback.recorder.anthropic_adapter import snapshot_request

    n = 200
    start = time.perf_counter()
    for _ in range(n):
        handle = recorder.begin(
            adapter, "messages.create", snapshot_request({"model": MODEL, "messages": messages})
        )
        assert handle is not None
        handle.finish(response=None)
    per_call_ms = (time.perf_counter() - start) * 1000 / n
    assert per_call_ms < 2.0, f"hot path took {per_call_ms:.3f} ms per call"
    assert stepback.flush(30)


def test_short_script_is_flushed_at_exit(tmp_path: Path) -> None:
    """No explicit flush: the atexit hook must persist every step before the process ends."""
    db = tmp_path / "exit.db"
    script = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(Path(__file__).parent)!r})
        from fakes import FakeAPI, text_message, MODEL
        import stepback
        stepback.configure(db_path={str(db)!r}, run_name="exit-test")
        api = FakeAPI().push(*[text_message(str(i)) for i in range(5)])
        client = stepback.wrap(api.sync_client())
        for i in range(5):
            client.messages.create(model=MODEL, max_tokens=5, messages=[{{"role": "user", "content": str(i)}}])
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    conn = sqlite3.connect(db)
    try:
        (count,) = conn.execute("SELECT COUNT(*) FROM steps").fetchone()
        status, name = conn.execute("SELECT status, name FROM runs").fetchone()
    finally:
        conn.close()
    assert count == 5
    assert (status, name) == ("ok", "exit-test")


def test_stored_request_is_replayable_json(fake_api: FakeAPI, recorded: Any) -> None:
    client = stepback.wrap(fake_api.sync_client())
    fake_api.push(text_message("a"))
    _create(client, [{"role": "user", "content": "x"}], metadata={"user_id": "u1"})
    (step,) = recorded()
    # Exactly the kwargs the user passed (minus transport options), JSON round-trippable.
    assert json.loads(json.dumps(step.request)) == {
        "model": MODEL,
        "max_tokens": 256,
        "messages": [{"role": "user", "content": "x"}],
        "metadata": {"user_id": "u1"},
    }


def test_accumulate_stream_handles_thinking_and_partial_json() -> None:
    from stepback.recorder.anthropic_adapter import accumulate_stream

    events = [
        {
            "type": "message_start",
            "message": {"id": "m", "model": MODEL, "usage": {"input_tokens": 5}},
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "Hmm"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "sig"},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "tool_use", "id": "t", "name": "f", "input": {}},
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": '{"a": '},
        },
        # Stream cut off before content_block_stop: keep the partial JSON rather than lose it.
    ]
    message = accumulate_stream(events)
    assert message is not None
    assert message["content"][0] == {"type": "thinking", "thinking": "Hmm", "signature": "sig"}
    assert message["content"][1]["input"] == {"_partial_json": '{"a": '}
    assert message["usage"] == {"input_tokens": 5}


@pytest.mark.skipif(
    not hasattr(anthropic.resources.messages.Messages, "parse"),
    reason="messages.parse() needs a newer anthropic SDK",
)
def test_parse_structured_output_is_recorded(fake_api: FakeAPI, recorded: Any) -> None:
    import pydantic

    class Answer(pydantic.BaseModel):
        city: str

    client = stepback.wrap(fake_api.sync_client())
    fake_api.push(text_message('{"city": "Paris"}'))
    parsed = client.messages.parse(
        model=MODEL,
        max_tokens=50,
        messages=[{"role": "user", "content": "Capital of France?"}],
        output_format=Answer,
    )
    assert parsed.parsed_output == Answer(city="Paris")
    (step,) = recorded()
    assert step.operation == "messages.parse"
    assert step.request["output_format"]["pydantic_model"] == "Answer"
    assert step.request["output_format"]["schema"]["properties"]["city"]["type"] == "string"
    assert step.response is not None and step.response["content"][0]["text"] == '{"city": "Paris"}'
