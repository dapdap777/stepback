from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import anthropic
import pytest
from click.testing import CliRunner

import stepback
from fakes import MODEL, FakeAPI, error_body, stream_events, tool_use_message
from stepback.cli import cli
from stepback.cli.render import fmt_ago, fmt_cost, fmt_ms, one_line
from stepback.storage import Run, Step, TraceStore


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def invoke(runner: CliRunner, db_path: Path, *args: str, width: int = 120) -> Any:
    result = runner.invoke(
        cli, ["--db", str(db_path), *args], env={"COLUMNS": str(width)}, catch_exceptions=False
    )
    return result


@pytest.fixture
def agent_run(fake_api: FakeAPI, db_path: Path) -> str:
    """A recorded 3-step tool-using run, plus a second run with an API error."""
    client = stepback.wrap(fake_api.sync_client())
    fake_api.push(
        tool_use_message("toolu_1", "get_weather", {"city": "Paris"}),
        stream_events(["It is ", "sunny [bold]in[/] Paris."]),
        (529, error_body("Overloaded", "overloaded_error")),
    )
    with stepback.run("weather-agent") as r:
        messages: list[Any] = [{"role": "user", "content": "Weather in Paris?"}]
        first = client.messages.create(model=MODEL, max_tokens=50, messages=messages)
        messages += [
            {"role": "assistant", "content": first.content},
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "Sunny, 24C"}
                ],
            },
        ]
        for _ in client.messages.create(model=MODEL, max_tokens=50, messages=messages, stream=True):
            pass
    with stepback.run("flaky"), pytest.raises(anthropic.APIStatusError):
        client.messages.create(
            model=MODEL, max_tokens=5, messages=[{"role": "user", "content": "x"}]
        )
    assert stepback.flush(5)
    assert r.id is None  # the scope's id is cleared on exit
    with TraceStore(db_path) as store:
        run_id = store.find_run_by_name("weather-agent")
    assert run_id
    return run_id


def test_ls_lists_runs(runner: CliRunner, db_path: Path, agent_run: str) -> None:
    result = invoke(runner, db_path, "ls")
    assert result.exit_code == 0
    assert agent_run in result.output
    assert "weather-agent" in result.output and "flaky" in result.output
    assert "1✗" in result.output  # the flaky run's failed step
    assert "$" in result.output


def test_ls_narrow_terminal_keeps_cost_column(
    runner: CliRunner, db_path: Path, agent_run: str
) -> None:
    with TraceStore(db_path) as store:
        store.create_run(
            Run(id="run_narrow000001", name="weather-agent (mock)", created_at=time.time())
        )
    result = invoke(runner, db_path, "ls", width=80)
    assert "…" not in "".join(line for line in result.output.splitlines() if "Steps" in line)
    lines = result.output.splitlines()
    header = next(line for line in lines if line.strip().startswith("Run"))
    assert "Cost" in header
    assert all(len(line) <= 80 for line in lines)


def test_ls_json(runner: CliRunner, db_path: Path, agent_run: str) -> None:
    data = json.loads(invoke(runner, db_path, "ls", "--json").output)
    by_name = {r["name"]: r for r in data}
    assert by_name["weather-agent"]["step_count"] == 2
    assert by_name["flaky"]["error_count"] == 1


def test_show_tree(runner: CliRunner, db_path: Path, agent_run: str) -> None:
    result = invoke(runner, db_path, "show", agent_run)
    out = result.output
    assert result.exit_code == 0
    assert "weather-agent" in out and agent_run in out
    assert "#0" in out and "#1" in out
    assert 'get_weather(city="Paris")' in out
    assert "→ result" in out and "Sunny, 24C" in out
    assert "Weather in Paris?" in out
    # Rich markup inside model output is printed literally, not interpreted.
    assert "sunny [bold]in[/] Paris." in out
    assert "ttft" in out


def test_show_resolves_prefix_name_and_latest(
    runner: CliRunner, db_path: Path, agent_run: str
) -> None:
    by_prefix = invoke(runner, db_path, "show", agent_run[:8]).output
    by_name = invoke(runner, db_path, "show", "weather-agent").output
    assert agent_run in by_prefix and agent_run in by_name
    latest = invoke(runner, db_path, "show").output
    assert "flaky" in latest
    assert "overloaded_error: Overloaded" in latest


def test_show_unknown_run(runner: CliRunner, db_path: Path, agent_run: str) -> None:
    result = runner.invoke(cli, ["--db", str(db_path), "show", "nope"])
    assert result.exit_code != 0
    assert "no run id or name matches 'nope'" in result.output


def test_show_step_detail(runner: CliRunner, db_path: Path, agent_run: str) -> None:
    out = invoke(runner, db_path, "show", agent_run, "--step", "1").output
    assert "step #1" in out
    assert "[0] user" in out and "[1] assistant" in out and "[2] user" in out
    assert "→ result toolu_1" in out
    assert "response" in out
    bad = runner.invoke(cli, ["--db", str(db_path), "show", agent_run, "--step", "9"])
    assert bad.exit_code != 0 and "no step #9" in bad.output


def test_show_json(runner: CliRunner, db_path: Path, agent_run: str) -> None:
    data = json.loads(invoke(runner, db_path, "show", agent_run, "--json").output)
    assert data["id"] == agent_run
    assert [s["idx"] for s in data["steps"]] == [0, 1]
    assert data["steps"][0]["tool_results"][0]["content"] == "Sunny, 24C"


def test_show_forked_run(runner: CliRunner, db_path: Path, agent_run: str) -> None:
    with TraceStore(db_path) as store:
        original = store.get_steps(agent_run)[1]
        store.create_run(
            Run(
                id="run_fork00000001",
                name="weather-agent (replay #1)",
                created_at=time.time(),
                parent_run_id=agent_run,
                forked_from_step_id=original.id,
            )
        )
        store.insert_step(
            Step(
                id="step_fork1",
                run_id="run_fork00000001",
                idx=1,
                provider="anthropic",
                request=original.request,
                started_at=time.time(),
                parent_step_id=original.parent_step_id,
                model=MODEL,
                response={"content": [{"type": "text", "text": "Replayed answer"}]},
                is_replay=True,
            )
        )
    fork = invoke(runner, db_path, "show", "run_fork").output
    assert "forked from" in fork and agent_run in fork
    assert "(inherited)" in fork and "⟲ replay" in fork and "Replayed answer" in fork
    parent = invoke(runner, db_path, "show", agent_run).output
    assert "⟲ fork" in parent and "run_fork00000001" in parent


def test_rm(runner: CliRunner, db_path: Path, agent_run: str) -> None:
    result = invoke(runner, db_path, "rm", agent_run, "--yes")
    assert "Deleted 1 run" in result.output
    with TraceStore(db_path) as store:
        assert store.get_run(agent_run) is None


def test_missing_database_is_friendly(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(cli, ["--db", str(tmp_path / "none.db"), "ls"])
    assert result.exit_code == 1
    assert "No traces yet" in result.output
    assert "stepback.wrap" in result.output
    assert not (tmp_path / "none.db").exists()


def test_prices_command(runner: CliRunner, db_path: Path) -> None:
    (db_path.parent).mkdir(parents=True, exist_ok=True)
    (db_path.parent / "prices.toml").write_text('[models."my-model"]\ninput = 1\noutput = 2\n')
    out = invoke(runner, db_path, "prices", width=200).output
    assert "claude-opus-5-5" in out and "built-in" in out
    assert "my-model" in out and "prices.toml" in out
    one = invoke(runner, db_path, "prices", "--model", "claude-opus-5-5-20270101").output
    assert "input=4.0" in one
    missing = runner.invoke(cli, ["--db", str(db_path), "prices", "-m", "gpt-x"])
    assert missing.exit_code != 0


def test_version(runner: CliRunner) -> None:
    assert stepback.__version__ in runner.invoke(cli, ["--version"]).output


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, "—"), (0, "$0"), (0.00001, "<$0.0001"), (0.01234, "$0.0123"), (12.5, "$12.50")],
)
def test_fmt_cost(value: float | None, expected: str) -> None:
    assert fmt_cost(value) == expected


def test_formatters() -> None:
    assert fmt_ms(None) == "—"
    assert fmt_ms(250) == "250ms"
    assert fmt_ms(2400) == "2.40s"
    assert fmt_ms(65_000) == "1m 05s"
    assert fmt_ago(100, now=130) == "30s ago"
    assert fmt_ago(0, now=7200) == "2h ago"
    assert one_line("a\n  b   c", 100) == "a b c"
    assert one_line("abcdefghij", 5) == "abcd…"


def test_rm_parent_with_forks_needs_cascade(
    runner: CliRunner, db_path: Path, agent_run: str
) -> None:
    with TraceStore(db_path) as store:
        store.create_run(
            Run(id="run_child", name="child", created_at=time.time(), parent_run_id=agent_run)
        )
    refused = runner.invoke(cli, ["--db", str(db_path), "rm", agent_run, "-y"])
    assert refused.exit_code != 0 and "--cascade" in refused.output
    ok = invoke(runner, db_path, "rm", agent_run, "-y", "--cascade")
    assert "Deleted 1 run" in ok.output
    with TraceStore(db_path) as store:
        assert store.get_run("run_child") is None
