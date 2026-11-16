from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

import stepline
from fakes import MODEL, FakeAPI, text_message, tool_use_message
from stepline.server import create_app
from stepline.storage import Run, Step, TraceStore

BASE = "http://127.0.0.1:7777"
CLIENT = {"X-Stepline-Client": "1"}


def make_step(run_id: str, idx: int, step_id: str, parent: str | None, **kw: Any) -> Step:
    data: dict[str, Any] = dict(
        id=step_id,
        run_id=run_id,
        idx=idx,
        provider="anthropic",
        request={"model": MODEL, "messages": [{"role": "user", "content": f"q{idx}"}]},
        response={"content": [{"type": "text", "text": f"a{idx}"}]},
        started_at=1000.0 + idx,
        parent_step_id=parent,
        model=MODEL,
        input_tokens=10,
        output_tokens=5,
        cost_usd=0.001,
        latency_ms=100.0,
    )
    data.update(kw)
    return Step(**data)


def chain(store: TraceStore, run_id: str, n: int, prefix: str, parent: str | None = None) -> None:
    for i in range(n):
        sid = f"{prefix}{i}"
        store.insert_step(make_step(run_id, i, sid, parent))
        parent = sid


@pytest.fixture
def api(db_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(db_path), base_url=BASE) as client:
        yield client


@pytest.fixture
def forked(db_path: Path) -> Path:
    """main: m0..m3; fork replays m2 (sibling f2, parent m1); fork2 identical replay of m3."""
    with TraceStore(db_path) as store:
        store.create_run(Run(id="run_main", name="main", created_at=1.0))
        chain(store, "run_main", 4, "m")
        store.create_run(
            Run(
                id="run_fork",
                name="main (replay #2)",
                created_at=2.0,
                parent_run_id="run_main",
                forked_from_step_id="m2",
            )
        )
        store.insert_step(
            make_step(
                "run_fork",
                2,
                "f2",
                "m1",
                is_replay=True,
                request={"model": MODEL, "messages": [{"role": "user", "content": "edited"}]},
            )
        )
        same = store.get_step("m3")
        assert same is not None
        store.create_run(
            Run(
                id="run_same",
                name="main (replay #3)",
                created_at=3.0,
                parent_run_id="run_main",
                forked_from_step_id="m3",
            )
        )
        store.insert_step(
            make_step("run_same", 3, "s3", "m2", request=same.request, response=same.response)
        )
    return db_path


# ---------------------------------------------------------------------- runs
def test_health(api: TestClient, db_path: Path) -> None:
    body = api.get("/api/health").json()
    assert body["version"] == stepline.__version__
    assert body["db_path"] == str(db_path)


def test_run_pagination_with_cursor(api: TestClient, db_path: Path) -> None:
    with TraceStore(db_path) as store:
        for i in range(7):
            store.create_run(Run(id=f"run_p{i}", name=f"job-{i}", created_at=100.0 + i))
        store.create_run(Run(id="run_tie", name="tie", created_at=106.0))  # same timestamp
    seen: list[str] = []
    cursor = None
    while True:
        params: dict[str, Any] = {"limit": 3}
        if cursor:
            params["cursor"] = cursor
        page = api.get("/api/runs", params=params).json()
        assert page["total"] == 8
        seen += [r["id"] for r in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) == 8
    assert seen[:2] == ["run_tie", "run_p6"]  # newest first, ties broken by id
    assert seen[-1] == "run_p0"


def test_run_list_filters(api: TestClient, forked: Path) -> None:
    by_name = api.get("/api/runs", params={"q": "replay"}).json()
    assert {r["id"] for r in by_name["items"]} == {"run_fork", "run_same"}
    assert by_name["total"] == 2
    roots = api.get("/api/runs", params={"include_forks": False}).json()
    assert [r["id"] for r in roots["items"]] == ["run_main"]
    assert roots["items"][0]["fork_count"] == 2
    like = api.get("/api/runs", params={"q": "%"}).json()  # LIKE wildcard is literal
    assert like["total"] == 0
    assert api.get("/api/runs", params={"status": "running"}).json()["total"] == 3


def test_run_list_validation(api: TestClient) -> None:
    assert api.get("/api/runs", params={"limit": 0}).status_code == 422
    assert api.get("/api/runs", params={"limit": 201}).status_code == 422
    assert api.get("/api/runs", params={"status": "bogus"}).status_code == 422
    bad = api.get("/api/runs", params={"cursor": "!!notacursor"})
    assert bad.status_code == 400


def test_run_detail(api: TestClient, forked: Path) -> None:
    main = api.get("/api/runs/run_main").json()
    assert main["run"]["step_count"] == 4
    assert {f["id"] for f in main["forks"]} == {"run_fork", "run_same"}
    assert main["parent"] is None and main["inherited_steps"] == 0 and main["own_steps"] == 4
    fork = api.get("/api/runs/run_fork").json()
    assert fork["parent"] == {"id": "run_main", "name": "main"}
    assert fork["forked_from_step"] == {"id": "m2", "run_id": "run_main", "idx": 2}
    assert fork["inherited_steps"] == 2 and fork["own_steps"] == 1
    assert api.get("/api/runs/nope").status_code == 404


# ---------------------------------------------------------------------- steps
def test_step_list_is_paginated_and_has_no_bodies(api: TestClient, db_path: Path) -> None:
    with TraceStore(db_path) as store:
        store.create_run(Run(id="run_long", name="long", created_at=1.0))
        chain(store, "run_long", 250, "l")
    first = api.get("/api/runs/run_long/steps", params={"limit": 100}).json()
    assert first["total"] == 250 and len(first["items"]) == 100
    assert [s["idx"] for s in first["items"]][:3] == [0, 1, 2]
    last = api.get("/api/runs/run_long/steps", params={"offset": 200, "limit": 100}).json()
    assert [s["idx"] for s in last["items"]] == list(range(200, 250))
    item = first["items"][5]
    assert "request" not in item and "response" not in item
    assert item["input_preview"] == "q5" and item["output_preview"] == "a5"
    assert item["request_hash"] and item["message_count"] == 1
    assert api.get("/api/runs/run_long/steps", params={"limit": 501}).status_code == 422
    assert api.get("/api/runs/nope/steps").status_code == 404


def test_fork_step_list_includes_inherited_across_pages(api: TestClient, forked: Path) -> None:
    full = api.get("/api/runs/run_fork/steps").json()
    assert [(s["id"], s["inherited"]) for s in full["items"]] == [
        ("m0", True),
        ("m1", True),
        ("f2", False),
    ]
    assert full["total"] == 3
    page2 = api.get("/api/runs/run_fork/steps", params={"offset": 1, "limit": 1}).json()
    assert [s["id"] for s in page2["items"]] == ["m1"]
    page3 = api.get("/api/runs/run_fork/steps", params={"offset": 2, "limit": 5}).json()
    assert [s["id"] for s in page3["items"]] == ["f2"]


def test_step_detail_has_full_bodies(api: TestClient, fake_api: FakeAPI, db_path: Path) -> None:
    client = stepline.wrap(fake_api.sync_client())
    fake_api.push(tool_use_message("toolu_1", "lookup", {"q": "x"}), text_message("done"))
    messages: list[Any] = [{"role": "user", "content": "go"}]
    first = client.messages.create(model=MODEL, max_tokens=10, messages=messages)
    messages += [
        {"role": "assistant", "content": first.content},
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "42"}],
        },
    ]
    client.messages.create(model=MODEL, max_tokens=10, messages=messages)
    assert stepline.flush(5)

    run_id = api.get("/api/runs").json()["items"][0]["id"]
    s0, s1 = api.get(f"/api/runs/{run_id}/steps").json()["items"]
    assert s0["tool_calls"] == [
        {
            "id": "toolu_1",
            "name": "lookup",
            "input_preview": 'q="x"',
            "has_result": True,
            "is_error": False,
        }
    ]
    assert s0["output_preview"] == "Let me check."
    assert s1["input_preview"] == "[1 tool result]"

    detail = api.get(f"/api/steps/{s1['id']}").json()
    assert detail["request"]["messages"][2]["content"][0]["content"] == "42"
    assert detail["response"]["content"][0]["text"] == "done"
    assert detail["parent_message_count"] == 1  # messages[2:] are new in this step
    first_detail = api.get(f"/api/steps/{s0['id']}").json()
    assert first_detail["tool_results"] == [
        {"tool_use_id": "toolu_1", "content": "42", "is_error": False}
    ]
    assert first_detail["parent_message_count"] is None
    assert api.get("/api/steps/step_nope").status_code == 404


def test_large_run_lists_quickly_and_compactly(api: TestClient, db_path: Path) -> None:
    """Thousands of steps with growing histories: list pages stay small and fast."""
    n = 3000
    history: list[dict[str, Any]] = []
    with TraceStore(db_path) as store:
        store.create_run(Run(id="run_big", name="big", created_at=1.0))
        parent = None
        for i in range(n):
            history.append({"role": "user", "content": f"message {i} " + "x" * 200})
            # Store a realistic, growing request but only keep the last 40 messages in memory
            # for speed; the bodies are still large.
            store.insert_step(
                make_step(
                    "run_big",
                    i,
                    f"b{i}",
                    parent,
                    request={"model": MODEL, "messages": history[-40:]},
                )
            )
            parent = f"b{i}"
    for offset in (0, 1500, 2900):
        start = time.perf_counter()
        response = api.get("/api/runs/run_big/steps", params={"offset": offset, "limit": 100})
        elapsed = time.perf_counter() - start
        assert response.status_code == 200
        assert len(response.json()["items"]) == 100
        assert elapsed < 0.5, f"page at offset {offset} took {elapsed:.3f}s"
        assert len(response.content) / 100 < 2000, "list items must not carry message bodies"
    start = time.perf_counter()
    assert api.get("/api/runs").status_code == 200
    assert time.perf_counter() - start < 0.5


# ---------------------------------------------------------------------- diff
def test_diff_parent_vs_fork(api: TestClient, forked: Path) -> None:
    diff = api.get("/api/diff", params={"a": "run_main", "b": "run_fork"}).json()
    statuses = [(p["position"], p["status"]) for p in diff["pairs"]]
    assert statuses == [(0, "shared"), (1, "shared"), (2, "changed"), (3, "removed")]
    changed = diff["pairs"][2]
    assert changed["request_changed"] and not changed["response_changed"]
    assert changed["a"]["id"] == "m2" and changed["b"]["id"] == "f2"
    assert diff["first_divergence"] == 2
    assert diff["totals_a"]["steps"] == 4 and diff["totals_b"]["steps"] == 3
    assert diff["total_pairs"] == 4


def test_diff_identical_replay_is_same(api: TestClient, forked: Path) -> None:
    diff = api.get("/api/diff", params={"a": "run_main", "b": "run_same"}).json()
    assert [p["status"] for p in diff["pairs"]] == ["shared", "shared", "shared", "same"]
    assert diff["first_divergence"] is None


def test_diff_pagination_and_errors(api: TestClient, forked: Path) -> None:
    page = api.get("/api/diff", params={"a": "run_main", "b": "run_fork", "offset": 3}).json()
    assert [p["position"] for p in page["pairs"]] == [3]
    assert api.get("/api/diff", params={"a": "run_main", "b": "nope"}).status_code == 404
    assert api.get("/api/diff", params={"a": "run_main"}).status_code == 422


# ---------------------------------------------------------------------- delete
def test_delete_refuses_when_forks_exist(api: TestClient, forked: Path) -> None:
    response = api.delete("/api/runs/run_main", headers=CLIENT)
    assert response.status_code == 409
    assert "cascade=true" in response.json()["detail"]
    assert api.delete("/api/runs/run_main?cascade=true", headers=CLIENT).status_code == 204
    assert api.get("/api/runs").json()["total"] == 0
    assert api.delete("/api/runs/run_main", headers=CLIENT).status_code == 404


# ---------------------------------------------------------------------- SPA + schema
def test_ui_not_built_page(db_path: Path, tmp_path: Path) -> None:
    app = create_app(db_path, static_dir=tmp_path / "no-build")
    with TestClient(app, base_url=BASE) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "npm run build" in page.text
        assert "<style" not in page.text and "<script" not in page.text  # CSP-clean
        assert client.get("/runs/run_x").status_code == 200
        assert client.get("/api/nope").status_code == 404


def test_serves_built_spa(db_path: Path, tmp_path: Path) -> None:
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<html>SPA</html>")
    (static / "assets" / "app.js").write_text("console.log(1)")
    (tmp_path / "secret.txt").write_text("outside")
    with TestClient(create_app(db_path, static_dir=static), base_url=BASE) as client:
        assert client.get("/").text == "<html>SPA</html>"
        assert client.get("/runs/run_123/steps/4").text == "<html>SPA</html>"  # client routes
        asset = client.get("/assets/app.js")
        assert asset.text == "console.log(1)"
        assert "javascript" in asset.headers["content-type"]
        traversal = client.get("/..%2Fsecret.txt")
        assert "outside" not in traversal.text


def test_openapi_schema(api: TestClient) -> None:
    schema = api.get("/api/openapi.json").json()
    ops = {op["operationId"] for path in schema["paths"].values() for op in path.values()}
    assert ops == {
        "getHealth",
        "listRuns",
        "getRun",
        "deleteRun",
        "listSteps",
        "getStep",
        "diffRuns",
        "getReplayStatus",
        "estimateReplay",
        "replayStep",
    }
    components = schema["components"]["schemas"]
    for name in ("RunPage", "RunDetail", "StepPage", "StepSummaryOut", "StepDetail", "RunDiff"):
        assert name in components
    # No CDN-backed docs pages.
    assert api.get("/docs").text != api.get("/api/openapi.json").text
    assert "swagger" not in api.get("/docs").text.lower()


def test_openapi_cli_command() -> None:
    from stepline.cli import cli

    result = CliRunner().invoke(cli, ["openapi"])
    assert result.exit_code == 0
    assert json.loads(result.output)["info"]["title"] == "Stepline"


def test_diff_aligns_around_steps_missing_from_the_fork(api: TestClient, db_path: Path) -> None:
    """A failed attempt is in the parent's history but not the fork's: later steps still align."""
    with TraceStore(db_path) as store:
        store.create_run(Run(id="run_par", name="par", created_at=1.0))
        store.insert_step(make_step("run_par", 0, "p0", None))
        store.insert_step(make_step("run_par", 1, "p1", "p0"))
        # p2 failed (no response); failed calls never become parents.
        store.insert_step(
            make_step(
                "run_par",
                2,
                "p2",
                "p1",
                response=None,
                error={"type": "OverloadedError"},
                request={"model": MODEL, "messages": [{"role": "user", "content": "q2"}]},
            )
        )
        store.insert_step(
            make_step(
                "run_par",
                3,
                "p3",
                "p1",
                # A retry sends exactly the same request as the failed attempt.
                request={"model": MODEL, "messages": [{"role": "user", "content": "q2"}]},
            )
        )
        store.insert_step(make_step("run_par", 4, "p4", "p3"))
        store.create_run(
            Run(
                id="run_kid",
                name="kid",
                created_at=2.0,
                parent_run_id="run_par",
                forked_from_step_id="p4",
            )
        )
        store.insert_step(
            make_step(
                "run_kid",
                4,
                "k4",
                "p3",
                is_replay=True,
                request={"model": MODEL, "messages": [{"role": "user", "content": "edited"}]},
            )
        )
    diff = api.get("/api/diff", params={"a": "run_par", "b": "run_kid"}).json()
    rows = [(p["a"] and p["a"]["id"], p["b"] and p["b"]["id"], p["status"]) for p in diff["pairs"]]
    assert rows == [
        ("p0", "p0", "shared"),
        ("p1", "p1", "shared"),
        ("p2", None, "removed"),
        ("p3", "p3", "shared"),
        ("p4", "k4", "changed"),
    ]
    assert diff["first_divergence"] == 2


def test_diff_of_independent_reruns_matches_identical_requests(
    api: TestClient, db_path: Path
) -> None:
    with TraceStore(db_path) as store:
        for run_id, answers in (
            ("run_r1", ["a0", "a1", "a2"]),
            ("run_r2", ["a0", "DIFFERENT", "a2"]),
        ):
            store.create_run(Run(id=run_id, name=run_id, created_at=1.0))
            parent = None
            for i, answer in enumerate(answers):
                sid = f"{run_id}_{i}"
                store.insert_step(
                    make_step(
                        run_id,
                        i,
                        sid,
                        parent,
                        response={"content": [{"type": "text", "text": answer}]},
                    )
                )
                parent = sid
    diff = api.get("/api/diff", params={"a": "run_r1", "b": "run_r2"}).json()
    assert [p["status"] for p in diff["pairs"]] == ["same", "changed", "same"]
    assert diff["pairs"][1]["response_changed"] and not diff["pairs"][1]["request_changed"]
