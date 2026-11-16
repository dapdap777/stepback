"""Replay: one explicit, paid call -> a new forked run. All API traffic is mocked."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import stepback
from fakes import MODEL, FakeAPI, error_body, text_message, tool_use_message
from stepback import replay
from stepback.server import create_app
from stepback.storage import TraceStore

BASE = "http://127.0.0.1:7777"
CLIENT = {"X-Stepback-Client": "1"}
SECRET = "sk-ant-api03-SUPERSECRETREPLAYKEY0123456789"


@pytest.fixture
def recorded_run(fake_api: FakeAPI, db_path: Path) -> dict[str, Any]:
    """A 2-step tool loop recorded through stepback.wrap()."""
    client = stepback.wrap(fake_api.sync_client())
    fake_api.push(
        tool_use_message("toolu_1", "get_weather", {"city": "Paris"}),
        text_message("It is sunny in Paris.", input_tokens=40, output_tokens=8),
    )
    with stepback.run("weather"):
        messages: list[Any] = [{"role": "user", "content": "Weather in Paris?"}]
        kwargs: dict[str, Any] = dict(model=MODEL, max_tokens=300, system="Be brief.")
        first = client.messages.create(messages=messages, **kwargs)
        messages += [
            {"role": "assistant", "content": first.content},
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "Sunny"}],
            },
        ]
        client.messages.create(messages=messages, **kwargs)
    assert stepback.flush(5)
    with TraceStore(db_path) as store:
        (summary,) = store.list_runs()
        steps = store.get_steps(summary.run.id)
    fake_api.requests.clear()
    fake_api.paths.clear()
    return {"run_id": summary.run.id, "steps": steps}


@pytest.fixture
def api(db_path: Path, fake_api: FakeAPI) -> Iterator[TestClient]:
    app = create_app(db_path, replay_client_factory=fake_api.sync_client)
    with TestClient(app, base_url=BASE) as client:
        yield client


def edited_request(step: Any, **changes: Any) -> dict[str, Any]:
    request = dict(step.request)
    request.update(changes)
    return request


def snapshot_rows(
    db_path: Path, run_id: str
) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    conn = sqlite3.connect(db_path)
    try:
        runs = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchall()
        steps = conn.execute(
            "SELECT * FROM steps WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
        return runs, steps
    finally:
        conn.close()


# ---------------------------------------------------------------------- happy path
def test_replay_creates_linked_fork(
    api: TestClient, fake_api: FakeAPI, recorded_run: dict[str, Any], db_path: Path
) -> None:
    source = recorded_run["steps"][1]
    fake_api.push(text_message("Sunny and 24C.", input_tokens=41, output_tokens=6))
    body = {
        "request": edited_request(source, system="Answer with the temperature."),
        "confirm": True,
    }
    response = api.post(f"/api/steps/{source.id}/replay", json=body, headers=CLIENT)
    assert response.status_code == 201, response.text
    out = response.json()
    assert out["status"] == "ok" and out["error"] is None
    assert out["changed"] == ["system"]

    # Exactly one paid request was sent, with the edit.
    assert len(fake_api.requests) == 1
    assert fake_api.requests[0]["system"] == "Answer with the temperature."
    assert "stream" not in fake_api.requests[0]

    with TraceStore(db_path) as store:
        fork = store.get_run(out["run_id"])
        assert fork is not None
        assert fork.parent_run_id == recorded_run["run_id"]
        assert fork.forked_from_step_id == source.id
        assert fork.name == "weather (replay #1)"
        assert fork.status == "ok"
        (step,) = store.get_steps(fork.id)
        assert step.id == out["step_id"]
        assert step.is_replay and step.idx == source.idx
        assert step.parent_step_id == source.parent_step_id  # sibling of the source step
        assert step.replay_overrides == {"source_step_id": source.id, "changed": ["system"]}
        assert step.response is not None and step.response["content"][0]["text"] == "Sunny and 24C."
        assert step.input_tokens == 41 and step.cost_usd == pytest.approx(41 * 4e-6 + 6 * 20e-6)
        # The fork's history: the inherited first step, then the replayed one.
        assert [s.id for s in store.get_lineage(fork.id)] == [recorded_run["steps"][0].id, step.id]

    # And the diff against the parent shows exactly that change.
    diff = api.get("/api/diff", params={"a": recorded_run["run_id"], "b": out["run_id"]}).json()
    assert [p["status"] for p in diff["pairs"]] == ["shared", "changed"]
    assert diff["pairs"][1]["request_changed"] and diff["pairs"][1]["response_changed"]


def test_source_run_is_byte_identical_after_replay(
    api: TestClient, fake_api: FakeAPI, recorded_run: dict[str, Any], db_path: Path
) -> None:
    run_id = recorded_run["run_id"]
    before = snapshot_rows(db_path, run_id)
    for i, source in enumerate(recorded_run["steps"]):
        fake_api.push(
            text_message(f"replay {i}"), (429, error_body("slow down", "rate_limit_error"))
        )
        for _ in range(2):  # one success and one failure per step
            api.post(
                f"/api/steps/{source.id}/replay",
                json={
                    "request": edited_request(source, model="claude-sonnet-5-5"),
                    "confirm": True,
                },
                headers=CLIENT,
            )
    assert snapshot_rows(db_path, run_id) == before
    assert len(before[1]) == 2
    with TraceStore(db_path) as store:
        assert len(store.list_forks(run_id)) == 4


def test_model_can_be_changed(
    api: TestClient, fake_api: FakeAPI, recorded_run: dict[str, Any]
) -> None:
    source = recorded_run["steps"][0]
    fake_api.push(text_message("ok"))
    out = api.post(
        f"/api/steps/{source.id}/replay",
        json={"request": edited_request(source, model="claude-haiku-5-5"), "confirm": True},
        headers=CLIENT,
    ).json()
    assert fake_api.requests[0]["model"] == "claude-haiku-5-5"
    assert out["changed"] == ["model"]
    step = api.get(f"/api/steps/{out['step_id']}").json()
    assert step["model"] == "claude-haiku-5-5"
    assert step["cost_usd"] == pytest.approx(12 * 0.1e-6 + 7 * 0.5e-6)  # haiku-5-5 pricing


def test_replay_of_a_replay(
    api: TestClient, fake_api: FakeAPI, recorded_run: dict[str, Any], db_path: Path
) -> None:
    source = recorded_run["steps"][1]
    fake_api.push(text_message("first"), text_message("second"))
    first = api.post(
        f"/api/steps/{source.id}/replay",
        json={"request": source.request, "confirm": True},
        headers=CLIENT,
    ).json()
    second = api.post(
        f"/api/steps/{first['step_id']}/replay",
        json={"request": edited_request(source, max_tokens=50), "confirm": True},
        headers=CLIENT,
    ).json()
    with TraceStore(db_path) as store:
        nested = store.get_run(second["run_id"])
        assert nested is not None and nested.parent_run_id == first["run_id"]
        assert nested.name == "weather (replay #1)"  # suffixes don't pile up
        assert len(store.get_lineage(second["run_id"])) == 2


def test_beta_requests_use_the_beta_endpoint(
    api: TestClient, fake_api: FakeAPI, recorded_run: dict[str, Any]
) -> None:
    source = recorded_run["steps"][0]
    fake_api.push(text_message("beta"))
    request = edited_request(source, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    out = api.post(
        f"/api/steps/{source.id}/replay", json={"request": request, "confirm": True}, headers=CLIENT
    ).json()
    assert out["status"] == "ok"
    assert "server-side-fallback-2026-07-01" in fake_api.request_headers[-1].get(
        "anthropic-beta", ""
    )
    assert fake_api.requests[0]["fallbacks"] == "default"


# ---------------------------------------------------------------------- explicit action only
def test_replay_requires_explicit_confirmation(
    api: TestClient, fake_api: FakeAPI, recorded_run: dict[str, Any], db_path: Path
) -> None:
    source = recorded_run["steps"][0]
    response = api.post(
        f"/api/steps/{source.id}/replay", json={"request": source.request}, headers=CLIENT
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "confirmation_required"
    assert fake_api.requests == [] and fake_api.count_requests == []
    with TraceStore(db_path) as store:
        assert store.count_runs() == 1


def test_replay_is_a_protected_post(
    api: TestClient, fake_api: FakeAPI, recorded_run: dict[str, Any]
) -> None:
    source = recorded_run["steps"][0]
    body = {"request": source.request, "confirm": True}
    assert (
        api.post(f"/api/steps/{source.id}/replay", json=body).status_code == 403
    )  # no client header
    cross = api.post(
        f"/api/steps/{source.id}/replay",
        json=body,
        headers={**CLIENT, "origin": "http://evil.example"},
    )
    assert cross.status_code == 403
    assert api.get(f"/api/steps/{source.id}/replay").status_code in (404, 405)
    assert fake_api.requests == []


def test_estimate_sends_nothing_billable(
    api: TestClient, fake_api: FakeAPI, recorded_run: dict[str, Any]
) -> None:
    source = recorded_run["steps"][1]
    fake_api.count_tokens_result = 2048
    est = api.post(
        f"/api/steps/{source.id}/replay/estimate",
        json={"request": edited_request(source, system="Shorter.", max_tokens=1000)},
        headers=CLIENT,
    ).json()
    assert est["model"] == MODEL and est["exact"] and est["method"] == "count_tokens"
    assert est["input_tokens"] == 2048 and est["max_tokens"] == 1000
    assert est["input_cost_usd"] == pytest.approx(2048 * 4e-6)
    assert est["max_output_cost_usd"] == pytest.approx(1000 * 20e-6)
    assert est["max_total_cost_usd"] == pytest.approx(2048 * 4e-6 + 1000 * 20e-6)
    assert est["changed"] == ["max_tokens", "system"]
    assert est["original_cost_usd"] == pytest.approx(source.cost_usd)
    assert fake_api.requests == []  # only count_tokens, never messages
    counted = fake_api.count_requests[0]
    assert counted["system"] == "Shorter." and "max_tokens" not in counted


def test_estimate_falls_back_to_local_count(
    api: TestClient, fake_api: FakeAPI, recorded_run: dict[str, Any]
) -> None:
    source = recorded_run["steps"][1]
    fake_api.count_tokens_result = (500, error_body("boom", "api_error"))
    est = api.post(
        f"/api/steps/{source.id}/replay/estimate",
        json={"request": edited_request(source, model="my-unknown-model")},
        headers=CLIENT,
    ).json()
    assert not est["exact"] and est["method"] == "scaled_from_recording"
    assert est["input_tokens"] > 0 and "Token counting failed" in est["note"]
    assert not est["price_known"] and est["max_total_cost_usd"] is None


# ---------------------------------------------------------------------- API errors -> failed step
@pytest.mark.parametrize(
    ("status", "etype", "message"),
    [
        (401, "authentication_error", "invalid x-api-key"),
        (
            429,
            "rate_limit_error",
            "Number of request tokens has exceeded your per-minute rate limit",
        ),
        (400, "invalid_request_error", "model: claude-retired-1 is not available"),
    ],
)
def test_api_errors_are_saved_as_failed_steps(
    api: TestClient,
    fake_api: FakeAPI,
    recorded_run: dict[str, Any],
    db_path: Path,
    status: int,
    etype: str,
    message: str,
) -> None:
    source = recorded_run["steps"][0]
    fake_api.push((status, error_body(message, etype)))
    response = api.post(
        f"/api/steps/{source.id}/replay",
        json={"request": edited_request(source, model="claude-retired-1"), "confirm": True},
        headers=CLIENT,
    )
    assert response.status_code == 201
    out = response.json()
    assert out["status"] == "error"
    assert out["error_message"] == f"{etype}: {message}"  # the API's own words
    assert out["error"]["status_code"] == status
    with TraceStore(db_path) as store:
        fork = store.get_run(out["run_id"])
        assert fork is not None and fork.status == "error"
        (step,) = store.get_steps(fork.id)
        assert step.error is not None and step.response is None and step.is_replay
        assert step.output_preview == f"{etype}: {message}"


def test_connection_errors_are_saved_too(db_path: Path, recorded_run: dict[str, Any]) -> None:
    import anthropic

    from fakes import httpx2

    def broken(request: Any) -> Any:
        raise httpx2.ConnectError("connection refused")

    def factory() -> Any:
        return anthropic.Anthropic(
            api_key="test",
            max_retries=0,
            http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(broken)),
        )

    with TraceStore(db_path) as store:
        result = replay.replay_step(
            store,
            recorded_run["steps"][0].id,
            recorded_run["steps"][0].request,
            client_factory=factory,
        )
    assert result.status == "error"
    assert result.error is not None and result.error["type"] == "APIConnectionError"


# ---------------------------------------------------------------------- credentials
def test_missing_key_gives_instructions_and_sends_nothing(
    db_path: Path, recorded_run: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    for var in replay.CREDENTIAL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(
        replay,
        "credential_status",
        lambda: replay.CredentialStatus(True, "x", False, None, replay.MISSING_CREDENTIALS_MESSAGE),
    )
    with TestClient(create_app(db_path), base_url=BASE) as api:  # default (env) factory
        status = api.get("/api/replay/status").json()
        assert status["credentials_configured"] is False
        assert "export ANTHROPIC_API_KEY" in status["message"]
        source = recorded_run["steps"][0]
        response = api.post(
            f"/api/steps/{source.id}/replay",
            json={"request": source.request, "confirm": True},
            headers=CLIENT,
        )
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert detail["code"] == "missing_credentials"
        assert "export ANTHROPIC_API_KEY=sk-ant-..." in detail["message"]
        # Estimating still works, locally.
        est = api.post(
            f"/api/steps/{source.id}/replay/estimate",
            json={"request": source.request},
            headers=CLIENT,
        ).json()
        assert not est["exact"] and est["note"] == "No API key: estimated locally."
    with TraceStore(db_path) as store:
        assert store.count_runs() == 1  # no fork was created


def test_sdk_auth_resolution_failure_creates_nothing(
    db_path: Path, recorded_run: dict[str, Any]
) -> None:
    import anthropic

    from fakes import httpx2

    def factory() -> Any:  # a client with no credentials at all
        return anthropic.Anthropic(
            api_key=None,
            max_retries=0,
            http_client=anthropic.DefaultHttpxClient(
                transport=httpx2.MockTransport(lambda r: httpx2.Response(500))
            ),
        )

    with TraceStore(db_path) as store:
        with pytest.raises(replay.MissingCredentials):
            replay.replay_step(
                store,
                recorded_run["steps"][0].id,
                recorded_run["steps"][0].request,
                client_factory=factory,
            )
        assert store.count_runs() == 1


def test_api_key_is_never_stored_logged_or_returned(
    db_path: Path,
    fake_api: FakeAPI,
    recorded_run: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: Any,
) -> None:
    import anthropic

    from fakes import DefaultHttpxClient, httpx2

    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)

    def env_factory() -> Any:  # reads the key from the environment, like the default factory
        return anthropic.Anthropic(
            max_retries=0,
            http_client=DefaultHttpxClient(transport=httpx2.MockTransport(fake_api.handler)),
        )

    caplog.set_level(logging.DEBUG)
    fake_api.push(
        text_message("fine"), (401, error_body("invalid x-api-key", "authentication_error"))
    )
    bodies: list[str] = []
    with TestClient(create_app(db_path, replay_client_factory=env_factory), base_url=BASE) as api:
        status = api.get("/api/replay/status")
        assert status.json()["credential_source"] == "ANTHROPIC_API_KEY"
        bodies.append(status.text)
        source = recorded_run["steps"][1]
        for _ in range(2):
            r = api.post(
                f"/api/steps/{source.id}/replay",
                json={"request": source.request, "confirm": True},
                headers=CLIENT,
            )
            bodies.append(r.text)
            bodies.append(api.get(f"/api/steps/{r.json()['step_id']}").text)
            bodies.append(api.get(f"/api/runs/{r.json()['run_id']}").text)
        bodies.append(
            api.post(
                f"/api/steps/{source.id}/replay/estimate",
                json={"request": source.request},
                headers=CLIENT,
            ).text
        )
    sent = fake_api.request_headers[-1]
    # it was used (SDK 2.x sends it as a bearer token, older SDKs as x-api-key)...
    assert SECRET in sent.get("x-api-key", "") + sent.get("authorization", "")
    for text in bodies:
        assert SECRET not in text  # ...but never returned
    assert SECRET not in caplog.text  # ...never logged
    stepback.flush(5)
    raw = b"".join(p.read_bytes() for p in db_path.parent.glob("traces.db*"))
    assert SECRET.encode() not in raw  # ...never stored


# ---------------------------------------------------------------------- validation
@pytest.mark.parametrize(
    ("changes", "path"),
    [
        ({"model": ""}, "model"),
        ({"max_tokens": 0}, "params.max_tokens"),
        ({"max_tokens": "100"}, "params.max_tokens"),
        ({"messages": []}, "messages"),
        ({"messages": [{"role": "robot", "content": "hi"}]}, "messages[0].role"),
        ({"messages": [{"role": "user", "content": 42}]}, "messages[0].content"),
        (
            {"messages": [{"role": "user", "content": [{"text": "no type"}]}]},
            "messages[0].content[0]",
        ),
        ({"tools": {"name": "x"}}, "tools"),
        ({"tools": [{"description": "nameless"}]}, "tools[0]"),
        ({"system": 5}, "system"),
        ({"extra_headers": {"x-api-key": "sk-ant-x"}}, "params.extra_headers"),
        ({"api_key": "sk-ant-x"}, "params.api_key"),
        ({"output_format": {"pydantic_model": "Answer", "schema": {}}}, "params.output_format"),
    ],
)
def test_invalid_edits_are_rejected_with_paths(
    api: TestClient,
    fake_api: FakeAPI,
    recorded_run: dict[str, Any],
    db_path: Path,
    changes: dict[str, Any],
    path: str,
) -> None:
    source = recorded_run["steps"][0]
    request = edited_request(source, **changes)
    for endpoint, body in (
        ("replay/estimate", {"request": request}),
        ("replay", {"request": request, "confirm": True}),
    ):
        response = api.post(f"/api/steps/{source.id}/{endpoint}", json=body, headers=CLIENT)
        assert response.status_code == 422, response.text
        detail = response.json()["detail"]
        assert detail["code"] == "invalid_request"
        assert path in [e["path"] for e in detail["errors"]]
    assert fake_api.requests == [] and fake_api.count_requests == []
    with TraceStore(db_path) as store:
        assert store.count_runs() == 1


def test_unknown_step(api: TestClient) -> None:
    body = {
        "request": {
            "model": MODEL,
            "max_tokens": 5,
            "messages": [{"role": "user", "content": "x"}],
        },
        "confirm": True,
    }
    assert api.post("/api/steps/step_nope/replay", json=body, headers=CLIENT).status_code == 404


def test_streamed_source_is_replayed_without_streaming(
    api: TestClient, fake_api: FakeAPI, recorded_run: dict[str, Any]
) -> None:
    source = recorded_run["steps"][0]
    fake_api.push(text_message("plain"))
    out = api.post(
        f"/api/steps/{source.id}/replay",
        json={"request": edited_request(source, stream=True), "confirm": True},
        headers=CLIENT,
    ).json()
    assert out["status"] == "ok" and "stream" not in fake_api.requests[0]
    assert out["changed"] == []  # stream is not an edit
