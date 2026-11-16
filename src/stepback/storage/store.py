"""Read/write access to runs and steps."""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .._util import dumps, loads
from ..config import resolve_db_path
from .db import connect
from .models import Run, RunSummary, Step

_STEP_COLUMNS = (
    "id",
    "run_id",
    "parent_step_id",
    "idx",
    "kind",
    "provider",
    "operation",
    "model",
    "request",
    "response",
    "tool_calls",
    "tool_results",
    "stop_reason",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cost_usd",
    "latency_ms",
    "ttft_ms",
    "streamed",
    "incomplete",
    "error",
    "started_at",
    "is_replay",
    "replay_overrides",
)
_JSON_STEP_FIELDS = {
    "request",
    "response",
    "tool_calls",
    "tool_results",
    "error",
    "replay_overrides",
}
_BOOL_STEP_FIELDS = {"streamed", "incomplete", "is_replay"}


class RunNotFound(KeyError):
    pass


class AmbiguousRunId(ValueError):
    def __init__(self, prefix: str, matches: list[str]) -> None:
        super().__init__(
            f"run id prefix {prefix!r} matches {len(matches)} runs: {', '.join(matches)}"
        )
        self.matches = matches


class TraceStore:
    """Thread-safe wrapper around the SQLite trace database."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path: Path = resolve_db_path(path)
        self._conn: sqlite3.Connection = connect(self.path)
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ lifecycle
    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> TraceStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _query(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _execute(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> int:
        with self._lock:
            return self._conn.execute(sql, params).rowcount

    # ------------------------------------------------------------------ runs
    def create_run(self, run: Run) -> None:
        self._execute(
            "INSERT OR IGNORE INTO runs (id, name, status, created_at, ended_at, parent_run_id,"
            " forked_from_step_id, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run.id,
                run.name,
                run.status,
                run.created_at,
                run.ended_at,
                run.parent_run_id,
                run.forked_from_step_id,
                dumps(run.metadata),
            ),
        )

    def end_run(self, run_id: str, status: str, ended_at: float) -> None:
        self._execute(
            "UPDATE runs SET status = ?, ended_at = ? WHERE id = ?", (status, ended_at, run_id)
        )

    def get_run(self, run_id: str) -> Run | None:
        rows = self._query("SELECT * FROM runs WHERE id = ?", (run_id,))
        return _row_to_run(rows[0]) if rows else None

    def resolve_run_id(self, prefix: str) -> str:
        """Accept a full id or a unique prefix (with or without the ``run_`` prefix)."""
        if self.get_run(prefix):
            return prefix
        candidates = [prefix] if prefix.startswith("run_") else [prefix, f"run_{prefix}"]
        matches: list[str] = []
        for candidate in candidates:
            escaped = candidate.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            for row in self._query(
                "SELECT id FROM runs WHERE id LIKE ? ESCAPE '\\' LIMIT 10", (escaped + "%",)
            ):
                if row["id"] not in matches:
                    matches.append(row["id"])
        if not matches:
            raise RunNotFound(prefix)
        if len(matches) > 1:
            raise AmbiguousRunId(prefix, matches)
        return matches[0]

    def find_run_by_name(self, name: str) -> str | None:
        """Id of the most recent run with exactly this name."""
        rows = self._query(
            "SELECT id FROM runs WHERE name = ? ORDER BY created_at DESC LIMIT 1", (name,)
        )
        return rows[0]["id"] if rows else None

    def list_runs(
        self, limit: int = 50, offset: int = 0, *, include_forks: bool = True
    ) -> list[RunSummary]:
        where = "" if include_forks else "WHERE r.parent_run_id IS NULL"
        rows = self._query(
            f"""
            SELECT r.*,
                   COUNT(s.id)                         AS step_count,
                   SUM(CASE WHEN s.error IS NOT NULL THEN 1 ELSE 0 END) AS error_count,
                   COALESCE(SUM(s.input_tokens), 0)    AS input_tokens,
                   COALESCE(SUM(s.output_tokens), 0)   AS output_tokens,
                   SUM(s.cost_usd)                     AS cost_usd,
                   COALESCE(SUM(s.latency_ms), 0)      AS latency_ms,
                   GROUP_CONCAT(DISTINCT s.model)      AS models,
                   (SELECT COUNT(*) FROM runs f WHERE f.parent_run_id = r.id) AS fork_count
            FROM runs r LEFT JOIN steps s ON s.run_id = r.id
            {where}
            GROUP BY r.id
            ORDER BY r.created_at DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        )
        return [_row_to_summary(row) for row in rows]

    def summarize(self, run_id: str) -> RunSummary | None:
        rows = self._query(
            """
            SELECT r.*,
                   COUNT(s.id) AS step_count,
                   SUM(CASE WHEN s.error IS NOT NULL THEN 1 ELSE 0 END) AS error_count,
                   COALESCE(SUM(s.input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(s.output_tokens), 0) AS output_tokens,
                   SUM(s.cost_usd) AS cost_usd,
                   COALESCE(SUM(s.latency_ms), 0) AS latency_ms,
                   GROUP_CONCAT(DISTINCT s.model) AS models,
                   (SELECT COUNT(*) FROM runs f WHERE f.parent_run_id = r.id) AS fork_count
            FROM runs r LEFT JOIN steps s ON s.run_id = r.id
            WHERE r.id = ?
            GROUP BY r.id
            """,
            (run_id,),
        )
        return _row_to_summary(rows[0]) if rows else None

    def list_forks(self, run_id: str) -> list[Run]:
        rows = self._query(
            "SELECT * FROM runs WHERE parent_run_id = ? ORDER BY created_at", (run_id,)
        )
        return [_row_to_run(row) for row in rows]

    def delete_run(self, run_id: str) -> bool:
        return self._execute("DELETE FROM runs WHERE id = ?", (run_id,)) > 0

    def count_runs(self) -> int:
        return int(self._query("SELECT COUNT(*) AS n FROM runs")[0]["n"])

    # ------------------------------------------------------------------ steps
    def insert_step(self, step: Step) -> None:
        values = []
        for column in _STEP_COLUMNS:
            value = getattr(step, column)
            if column in _JSON_STEP_FIELDS:
                value = None if value is None else dumps(value)
            elif column in _BOOL_STEP_FIELDS:
                value = int(bool(value))
            values.append(value)
        placeholders = ", ".join("?" for _ in _STEP_COLUMNS)
        self._execute(
            f"INSERT OR REPLACE INTO steps ({', '.join(_STEP_COLUMNS)}) VALUES ({placeholders})",
            values,
        )

    def add_tool_results(self, step_id: str, results: list[dict[str, Any]]) -> None:
        """Attach tool results to the step that requested them (merging by ``tool_use_id``)."""
        with self._lock:
            rows = self._query("SELECT tool_results FROM steps WHERE id = ?", (step_id,))
            if not rows:
                return
            existing: list[dict[str, Any]] = loads(rows[0]["tool_results"], []) or []
            by_id = {r.get("tool_use_id"): r for r in existing}
            for result in results:
                by_id[result.get("tool_use_id")] = result
            self._execute(
                "UPDATE steps SET tool_results = ? WHERE id = ?",
                (dumps(list(by_id.values())), step_id),
            )

    def get_step(self, step_id: str) -> Step | None:
        rows = self._query("SELECT * FROM steps WHERE id = ?", (step_id,))
        return _row_to_step(rows[0]) if rows else None

    def get_steps(self, run_id: str) -> list[Step]:
        rows = self._query(
            "SELECT * FROM steps WHERE run_id = ? ORDER BY idx, started_at", (run_id,)
        )
        return [_row_to_step(row) for row in rows]

    def get_ancestors(self, step_id: str | None) -> list[Step]:
        """Steps from the root down to (and including) ``step_id``, following ``parent_step_id``."""
        chain: list[Step] = []
        seen: set[str] = set()
        current = step_id
        while current and current not in seen:
            seen.add(current)
            step = self.get_step(current)
            if step is None:
                break
            chain.append(step)
            current = step.parent_step_id
        chain.reverse()
        return chain

    def get_lineage(self, run_id: str) -> list[Step]:
        """A run's full history: for forked runs, inherited ancestor steps followed by its own."""
        own = self.get_steps(run_id)
        run = self.get_run(run_id)
        if run is None or run.parent_run_id is None or not own:
            return own
        inherited = [s for s in self.get_ancestors(own[0].parent_step_id) if s.run_id != run_id]
        return inherited + own


# ---------------------------------------------------------------------- row mapping
def _row_to_run(row: sqlite3.Row) -> Run:
    return Run(
        id=row["id"],
        name=row["name"],
        status=row["status"],
        created_at=row["created_at"],
        ended_at=row["ended_at"],
        parent_run_id=row["parent_run_id"],
        forked_from_step_id=row["forked_from_step_id"],
        metadata=loads(row["metadata"], {}) or {},
    )


def _row_to_summary(row: sqlite3.Row) -> RunSummary:
    models = [m for m in (row["models"] or "").split(",") if m]
    return RunSummary(
        run=_row_to_run(row),
        step_count=row["step_count"] or 0,
        error_count=row["error_count"] or 0,
        input_tokens=row["input_tokens"] or 0,
        output_tokens=row["output_tokens"] or 0,
        cost_usd=row["cost_usd"],
        latency_ms=row["latency_ms"] or 0.0,
        models=models,
        fork_count=row["fork_count"] or 0,
    )


def _row_to_step(row: sqlite3.Row) -> Step:
    data: dict[str, Any] = {}
    for column in _STEP_COLUMNS:
        value = row[column]
        if column in _JSON_STEP_FIELDS:
            default: Any = [] if column in ("tool_calls", "tool_results") else None
            value = loads(value, default)
        elif column in _BOOL_STEP_FIELDS:
            value = bool(value)
        data[column] = value
    return Step(**data)
