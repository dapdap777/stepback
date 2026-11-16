"""The offline demo: the example agent against the bundled mock API."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from anthropic import Anthropic
from click.testing import CliRunner

from stepline.cli import cli
from stepline.demo import example_path
from stepline.demo.mock_api import SCRIPT, mock_http_client
from stepline.storage import TraceStore

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STEPLINE_DEMO_FAST", "1")


def test_mock_api_speaks_the_sdk_wire_format() -> None:
    client = Anthropic(api_key="offline", http_client=mock_http_client())
    messages = [{"role": "user", "content": "hi"}]
    first = client.messages.create(model="claude-opus-5-5", max_tokens=100, messages=messages)
    assert first.stop_reason == "tool_use"
    assert [b.type for b in first.content] == ["thinking", "text", "tool_use", "tool_use"]
    assert first.usage.input_tokens > 0

    later = [*messages, {"role": "assistant", "content": "x"}] * len(SCRIPT)
    with client.messages.stream(model="claude-opus-5-5", max_tokens=100, messages=later) as stream:
        text = "".join(stream.text_stream)
        final = stream.get_final_message()
    assert final.stop_reason == "end_turn" and "Lisbon" in text
    assert final.content[0].text == text


def test_stepline_demo_records_the_example_run(db_path: Path) -> None:
    result = CliRunner().invoke(cli, ["--db", str(db_path), "demo"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert "Go to Lisbon" in result.output and "stepline ui" in result.output
    with TraceStore(db_path) as store:
        (summary,) = store.list_runs()
        steps = store.get_steps(summary.run.id)
    assert summary.run.name == "weather-agent (mock)" and summary.run.status == "ok"
    assert len(steps) == 4
    assert all(s.streamed and s.ttft_ms is not None and s.cost_usd for s in steps)
    assert [len(s.tool_calls) for s in steps] == [2, 2, 1, 0]
    # Tool results were linked back to the calls, including the failing one.
    air = steps[1].tool_results
    assert {r["is_error"] for r in air} == {True, False}
    assert any("HTTP 503" in str(r["content"]) for r in air)
    assert [s.parent_step_id for s in steps[1:]] == [s.id for s in steps[:-1]]


def test_example_runs_with_one_command(tmp_path: Path) -> None:
    env = {**os.environ, "STEPLINE_DEMO_FAST": "1"}
    for var in ("STEPLINE_DB", "STEPLINE_DIR", "ANTHROPIC_API_KEY"):
        env.pop(var, None)
    proc = subprocess.run(
        [sys.executable, str(REPO / "examples" / "weather_agent.py"), "--mock"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "Recorded." in proc.stdout
    with TraceStore(tmp_path / ".stepline" / "traces.db") as store:
        (summary,) = store.list_runs()
    assert summary.step_count == 4


def test_example_is_found(tmp_path: Path) -> None:
    assert example_path().name == "weather_agent.py"
    assert example_path().read_text().count("client = stepline.wrap(client)") == 1


def test_ui_mock_mode_replays_offline(db_path: Path) -> None:
    from fastapi.testclient import TestClient

    from stepline.server import create_app

    CliRunner().invoke(cli, ["--db", str(db_path), "demo"], catch_exceptions=False)
    with TestClient(create_app(db_path, replay_mock=True), base_url="http://127.0.0.1:7777") as api:
        status = api.get("/api/replay/status").json()
        assert status["mock"] is True and status["credentials_configured"] is True
        run_id = api.get("/api/runs").json()["items"][0]["id"]
        last = api.get(f"/api/runs/{run_id}/steps").json()["items"][-1]
        source = api.get(f"/api/steps/{last['id']}").json()["request"]
        source["system"] = "Answer in one sentence."
        out = api.post(
            f"/api/steps/{last['id']}/replay",
            json={"request": source, "confirm": True},
            headers={"X-Stepline-Client": "1"},
        ).json()
        assert out["status"] == "ok" and out["changed"] == ["system"]
        replayed = api.get(f"/api/steps/{out['step_id']}").json()
        assert replayed["response"]["content"][0]["text"].startswith("Lisbon: sunny")
