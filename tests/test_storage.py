from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from stepline.storage import AmbiguousRunId, Run, RunNotFound, Step, TraceStore
from stepline.storage.db import SCHEMA_VERSION


def make_step(run_id: str, idx: int, step_id: str, parent: str | None = None, **kw: object) -> Step:
    defaults: dict[str, object] = dict(
        id=step_id,
        run_id=run_id,
        idx=idx,
        provider="anthropic",
        request={"model": "claude-opus-5-5", "messages": [{"role": "user", "content": str(idx)}]},
        started_at=time.time(),
        parent_step_id=parent,
        model="claude-opus-5-5",
        input_tokens=10,
        output_tokens=5,
        cost_usd=0.001,
        latency_ms=100.0,
    )
    defaults.update(kw)
    return Step(**defaults)  # type: ignore[arg-type]


def test_schema_created_with_version_and_gitignore(db_path: Path) -> None:
    with TraceStore(db_path) as store:
        assert store.count_runs() == 0
    conn = sqlite3.connect(db_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    conn.close()
    assert (db_path.parent / ".gitignore").read_text().strip().endswith("*")
    # Re-opening an existing database is a no-op migration.
    TraceStore(db_path).close()


def test_round_trip_run_and_steps(store: TraceStore) -> None:
    store.create_run(Run(id="run_a1", name="demo", created_at=1.0, metadata={"k": "v"}))
    store.insert_step(
        make_step(
            "run_a1",
            0,
            "step_1",
            tool_calls=[{"id": "t1", "name": "f", "input": {}}],
            response={"content": [{"type": "text", "text": "hi"}]},
            streamed=True,
            error={"type": "X", "message": "m"},
        )
    )
    store.insert_step(make_step("run_a1", 1, "step_2", parent="step_1"))
    run = store.get_run("run_a1")
    assert run is not None and run.metadata == {"k": "v"} and run.status == "running"

    steps = store.get_steps("run_a1")
    assert [s.id for s in steps] == ["step_1", "step_2"]
    first = steps[0]
    assert first.streamed is True and first.incomplete is False
    assert first.tool_calls == [{"id": "t1", "name": "f", "input": {}}]
    assert first.response == {"content": [{"type": "text", "text": "hi"}]}
    assert first.error == {"type": "X", "message": "m"}
    assert steps[1].parent_step_id == "step_1"
    assert store.get_step("step_2") == steps[1]
    assert store.get_step("missing") is None

    store.end_run("run_a1", "ok", 5.0)
    run = store.get_run("run_a1")
    assert run is not None and run.status == "ok" and run.ended_at == 5.0


def test_list_runs_aggregates(store: TraceStore) -> None:
    store.create_run(Run(id="run_old", name="old", created_at=1.0))
    store.create_run(Run(id="run_new", name="new", created_at=2.0))
    store.insert_step(make_step("run_new", 0, "s1"))
    store.insert_step(make_step("run_new", 1, "s2", error={"type": "E", "message": "x"}))
    store.insert_step(make_step("run_new", 2, "s3", model="claude-haiku-5-5", cost_usd=None))

    summaries = store.list_runs()
    assert [s.run.id for s in summaries] == ["run_new", "run_old"]
    new = summaries[0]
    assert new.step_count == 3
    assert new.error_count == 1
    assert new.input_tokens == 30 and new.output_tokens == 15
    assert new.cost_usd == pytest.approx(0.002)
    assert new.latency_ms == pytest.approx(300.0)
    assert set(new.models) == {"claude-opus-5-5", "claude-haiku-5-5"}
    assert summaries[1].step_count == 0 and summaries[1].cost_usd is None
    assert store.summarize("run_new") == new
    assert [s.run.id for s in store.list_runs(limit=1, offset=1)] == ["run_old"]


def test_resolve_run_id_prefix(store: TraceStore) -> None:
    store.create_run(Run(id="run_abc123", name="a", created_at=1.0))
    store.create_run(Run(id="run_abd456", name="b", created_at=2.0))
    assert store.resolve_run_id("run_abc123") == "run_abc123"
    assert store.resolve_run_id("abc") == "run_abc123"
    assert store.resolve_run_id("run_abd") == "run_abd456"
    with pytest.raises(AmbiguousRunId):
        store.resolve_run_id("ab")
    with pytest.raises(RunNotFound):
        store.resolve_run_id("zzz")
    # LIKE wildcards in user input are treated literally.
    with pytest.raises(RunNotFound):
        store.resolve_run_id("%")


def test_add_tool_results_merges_by_id(store: TraceStore) -> None:
    store.create_run(Run(id="run_t", name="t", created_at=1.0))
    store.insert_step(make_step("run_t", 0, "s1"))
    store.add_tool_results("s1", [{"tool_use_id": "a", "content": "1"}])
    store.add_tool_results(
        "s1", [{"tool_use_id": "b", "content": "2"}, {"tool_use_id": "a", "content": "1b"}]
    )
    step = store.get_step("s1")
    assert step is not None
    assert {r["tool_use_id"]: r["content"] for r in step.tool_results} == {"a": "1b", "b": "2"}
    store.add_tool_results("nope", [{"tool_use_id": "x"}])  # unknown step: ignored


def test_forked_run_lineage_and_tree(store: TraceStore) -> None:
    store.create_run(Run(id="run_main", name="main", created_at=1.0))
    for i, (sid, parent) in enumerate([("m0", None), ("m1", "m0"), ("m2", "m1"), ("m3", "m2")]):
        store.insert_step(make_step("run_main", i, sid, parent=parent))
    # Replay step m2 with edits: the fork's first step is a sibling of m2 (same parent m1).
    store.create_run(
        Run(
            id="run_fork",
            name="main (replay)",
            created_at=2.0,
            parent_run_id="run_main",
            forked_from_step_id="m2",
        )
    )
    store.insert_step(make_step("run_fork", 2, "f2", parent="m1", is_replay=True))
    # And a fork of the fork.
    store.create_run(
        Run(
            id="run_fork2",
            name="x",
            created_at=3.0,
            parent_run_id="run_fork",
            forked_from_step_id="f2",
        )
    )
    store.insert_step(make_step("run_fork2", 3, "g3", parent="f2", is_replay=True))

    assert [s.id for s in store.get_lineage("run_fork")] == ["m0", "m1", "f2"]
    assert [s.id for s in store.get_lineage("run_fork2")] == ["m0", "m1", "f2", "g3"]
    assert [s.id for s in store.get_lineage("run_main")] == ["m0", "m1", "m2", "m3"]
    assert [r.id for r in store.list_forks("run_main")] == ["run_fork"]
    assert store.summarize("run_main") is not None
    assert store.summarize("run_main").fork_count == 1  # type: ignore[union-attr]
    assert [s.run.id for s in store.list_runs(include_forks=False)] == ["run_main"]
    assert [s.id for s in store.get_ancestors("f2")] == ["m0", "m1", "f2"]


def test_delete_run_cascades(store: TraceStore) -> None:
    store.create_run(Run(id="run_d", name="d", created_at=1.0))
    store.insert_step(make_step("run_d", 0, "d0"))
    assert store.delete_run("run_d")
    assert store.get_step("d0") is None
    assert not store.delete_run("run_d")


def test_concurrent_reader_while_writing(db_path: Path) -> None:
    writer = TraceStore(db_path)
    reader = TraceStore(db_path)
    try:
        writer.create_run(Run(id="run_c", name="c", created_at=1.0))
        for i in range(20):
            writer.insert_step(make_step("run_c", i, f"c{i}"))
            assert len(reader.get_steps("run_c")) == i + 1
    finally:
        writer.close()
        reader.close()


def test_migrates_v1_database_and_backfills_digests(tmp_path: Path) -> None:
    import json as _json

    from stepline.storage.db import _MIGRATIONS

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    v1 = _MIGRATIONS[1]
    assert isinstance(v1, str)
    conn.executescript(v1)
    conn.execute("PRAGMA user_version = 1")
    conn.execute("INSERT INTO runs (id, name, created_at) VALUES ('run_old', 'old', 1.0)")
    request = {"model": "m", "messages": [{"role": "user", "content": "hello from v1"}]}
    response = {"content": [{"type": "text", "text": "hi back"}]}
    conn.execute(
        "INSERT INTO steps (id, run_id, idx, provider, request, response, started_at)"
        " VALUES ('s_old', 'run_old', 0, 'anthropic', ?, ?, 1.0)",
        (_json.dumps(request), _json.dumps(response)),
    )
    conn.commit()
    conn.close()

    with TraceStore(path) as store:
        step = store.get_step("s_old")
        assert step is not None
        assert step.input_preview == "hello from v1"
        assert step.output_preview == "hi back"
        assert step.request_hash and step.message_count == 1
        (summary,), _ = store.list_step_summaries("run_old")
        assert summary.input_preview == "hello from v1"
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    conn.close()


def test_page_runs_keyset(store: TraceStore) -> None:
    for i in range(5):
        store.create_run(Run(id=f"run_k{i}", name=f"k{i}", created_at=float(i)))
    first, cursor = store.page_runs(limit=2)
    assert [s.run.id for s in first] == ["run_k4", "run_k3"] and cursor
    # A run recorded between page loads doesn't shift the next page.
    store.create_run(Run(id="run_new", name="new", created_at=99.0))
    second, cursor2 = store.page_runs(limit=2, cursor=cursor)
    assert [s.run.id for s in second] == ["run_k2", "run_k1"]
    third, cursor3 = store.page_runs(limit=2, cursor=cursor2)
    assert [s.run.id for s in third] == ["run_k0"] and cursor3 is None
    with pytest.raises(ValueError):
        store.page_runs(cursor="garbage!")


def test_delete_parent_with_forks_requires_cascade(store: TraceStore) -> None:
    from stepline.storage import RunHasForks

    store.create_run(Run(id="run_p", name="p", created_at=1.0))
    store.insert_step(make_step("run_p", 0, "p0"))
    store.create_run(
        Run(id="run_c", name="c", created_at=2.0, parent_run_id="run_p", forked_from_step_id="p0")
    )
    store.insert_step(make_step("run_c", 0, "c0", is_replay=True))
    with pytest.raises(RunHasForks) as info:
        store.delete_run("run_p")
    assert info.value.fork_ids == ["run_c"]
    assert store.delete_run("run_p", cascade_forks=True)
    assert store.get_run("run_c") is None and store.get_step("c0") is None


def test_fork_without_own_steps_inherits_history(store: TraceStore) -> None:
    store.create_run(Run(id="run_h", name="h", created_at=1.0))
    for i, parent in enumerate([None, "h0", "h1"]):
        store.insert_step(make_step("run_h", i, f"h{i}", parent=parent))
    store.create_run(
        Run(
            id="run_empty",
            name="e",
            created_at=2.0,
            parent_run_id="run_h",
            forked_from_step_id="h2",
        )
    )
    assert [s.id for s in store.get_lineage("run_empty")] == ["h0", "h1"]
    assert store.count_lineage("run_empty") == (2, 0)
