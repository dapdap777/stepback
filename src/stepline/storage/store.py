"""Read/write access to runs and steps."""

from __future__ import annotations

import base64
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .._util import dumps, loads
from ..config import resolve_db_path
from .db import connect
from .digest import hash_json, input_preview, message_count, output_preview, tool_input_preview
from .models import Run, RunSummary, Step, StepSummary

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
    "input_preview",
    "output_preview",
    "request_hash",
    "response_hash",
    "message_count",
)
# Columns read for step lists: everything except the large JSON bodies.
_SUMMARY_COLUMNS = (
    "id",
    "run_id",
    "parent_step_id",
    "idx",
    "kind",
    "provider",
    "operation",
    "model",
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
    "is_replay",
    "started_at",
    "input_preview",
    "output_preview",
    "request_hash",
    "response_hash",
    "message_count",
    "error",
    "tool_calls",
    "tool_results",
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


class RunHasForks(ValueError):
    def __init__(self, run_id: str, fork_ids: list[str]) -> None:
        super().__init__(f"run {run_id} has {len(fork_ids)} fork(s): {', '.join(fork_ids)}")
        self.fork_ids = fork_ids


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
            escaped = _escape_like(candidate)
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
        """Newest runs first (offset pagination; the API uses :meth:`page_runs`)."""
        where = "" if include_forks else "WHERE parent_run_id IS NULL"
        return self._summaries(
            f"SELECT * FROM runs {where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )

    def page_runs(
        self,
        limit: int = 50,
        cursor: str | None = None,
        *,
        include_forks: bool = True,
        query: str | None = None,
        status: str | None = None,
    ) -> tuple[list[RunSummary], str | None]:
        """Keyset-paginated run list: returns ``(runs, next_cursor)``.

        Cursors stay valid while new runs are being recorded (no shifting offsets).
        """
        where, params = self._run_filters(include_forks, query, status)
        if cursor:
            created_at, run_id = decode_cursor(cursor)
            where.append("(created_at < ? OR (created_at = ? AND id < ?))")
            params += [created_at, created_at, run_id]
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        summaries = self._summaries(
            f"SELECT * FROM runs {clause} ORDER BY created_at DESC, id DESC LIMIT ?",
            (*params, limit + 1),
        )
        next_cursor = None
        if len(summaries) > limit:
            summaries = summaries[:limit]
            last = summaries[-1].run
            next_cursor = encode_cursor(last.created_at, last.id)
        return summaries, next_cursor

    def count_runs_matching(
        self,
        *,
        include_forks: bool = True,
        query: str | None = None,
        status: str | None = None,
    ) -> int:
        where, params = self._run_filters(include_forks, query, status)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        return int(self._query(f"SELECT COUNT(*) AS n FROM runs {clause}", params)[0]["n"])

    @staticmethod
    def _run_filters(
        include_forks: bool, query: str | None, status: str | None
    ) -> tuple[list[str], list[Any]]:
        where: list[str] = []
        params: list[Any] = []
        if not include_forks:
            where.append("parent_run_id IS NULL")
        if query:
            where.append("(name LIKE ? ESCAPE '\\' OR id LIKE ? ESCAPE '\\')")
            pattern = f"%{_escape_like(query)}%"
            params += [pattern, pattern]
        if status:
            where.append("status = ?")
            params.append(status)
        return where, params

    def _summaries(self, page_sql: str, params: tuple[Any, ...] | list[Any]) -> list[RunSummary]:
        """Aggregate step stats for just the runs selected by ``page_sql``."""
        rows = self._query(
            f"""
            WITH page AS ({page_sql})
            SELECT page.*,
                   COUNT(s.id)                                           AS step_count,
                   SUM(CASE WHEN s.error IS NOT NULL THEN 1 ELSE 0 END)  AS error_count,
                   COALESCE(SUM(s.input_tokens), 0)                      AS input_tokens,
                   COALESCE(SUM(s.output_tokens), 0)                     AS output_tokens,
                   SUM(s.cost_usd)                                       AS cost_usd,
                   COALESCE(SUM(s.latency_ms), 0)                        AS latency_ms,
                   GROUP_CONCAT(DISTINCT s.model)                        AS models,
                   (SELECT COUNT(*) FROM runs f WHERE f.parent_run_id = page.id) AS fork_count
            FROM page LEFT JOIN steps s ON s.run_id = page.id
            GROUP BY page.id
            ORDER BY page.created_at DESC, page.id DESC
            """,
            params,
        )
        return [_row_to_summary(row) for row in rows]

    def summarize(self, run_id: str) -> RunSummary | None:
        summaries = self._summaries("SELECT * FROM runs WHERE id = ?", (run_id,))
        return summaries[0] if summaries else None

    def list_forks(self, run_id: str) -> list[Run]:
        rows = self._query(
            "SELECT * FROM runs WHERE parent_run_id = ? ORDER BY created_at", (run_id,)
        )
        return [_row_to_run(row) for row in rows]

    def delete_run(self, run_id: str, *, cascade_forks: bool = False) -> bool:
        """Delete a run and its steps.

        Forked runs inherit steps from their parent, so deleting a parent is refused
        (:class:`RunHasForks`) unless ``cascade_forks`` is set, which deletes the forks too.
        """
        with self._lock:
            forks = [r.id for r in self.list_forks(run_id)]
            if forks and not cascade_forks:
                raise RunHasForks(run_id, forks)
            for fork_id in forks:
                self.delete_run(fork_id, cascade_forks=True)
            return self._execute("DELETE FROM runs WHERE id = ?", (run_id,)) > 0

    def count_runs(self) -> int:
        return int(self._query("SELECT COUNT(*) AS n FROM runs")[0]["n"])

    # ------------------------------------------------------------------ steps
    def insert_step(self, step: Step) -> None:
        fill_digest(step)
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
        ids = self._ancestor_ids(step_id)
        steps = {s.id: s for s in self._steps_by_ids(ids)}
        return [steps[i] for i in reversed(ids) if i in steps]

    def _ancestor_ids(self, step_id: str | None) -> list[str]:
        """``step_id`` and its ancestors, nearest first (one recursive query)."""
        if not step_id:
            return []
        rows = self._query(
            """
            WITH RECURSIVE chain(id, parent, depth) AS (
                SELECT id, parent_step_id, 0 FROM steps WHERE id = ?
                UNION ALL
                SELECT s.id, s.parent_step_id, chain.depth + 1
                FROM steps s JOIN chain ON s.id = chain.parent
                WHERE chain.depth < 100000
            )
            SELECT id FROM chain ORDER BY depth
            """,
            (step_id,),
        )
        return [row["id"] for row in rows]

    def _steps_by_ids(self, ids: list[str]) -> list[Step]:
        out: list[Step] = []
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            marks = ",".join("?" for _ in chunk)
            out += [
                _row_to_step(r)
                for r in self._query(f"SELECT * FROM steps WHERE id IN ({marks})", chunk)
            ]
        return out

    def _inherited_ids(self, run_id: str) -> list[str]:
        """For a forked run: ids of steps inherited from ancestor runs, oldest first."""
        run = self.get_run(run_id)
        if run is None or run.parent_run_id is None:
            return []
        first = self._query(
            "SELECT parent_step_id FROM steps WHERE run_id = ? ORDER BY idx, started_at LIMIT 1",
            (run_id,),
        )
        start = first[0]["parent_step_id"] if first else run.forked_from_step_id
        if not first and run.forked_from_step_id:
            # No steps of its own yet: inherit everything before the fork point.
            fork_step = self.get_step(run.forked_from_step_id)
            start = fork_step.parent_step_id if fork_step else None
        ids = list(reversed(self._ancestor_ids(start)))
        if not ids:
            return []
        own = {r["id"] for r in self._query("SELECT id FROM steps WHERE run_id = ?", (run_id,))}
        return [i for i in ids if i not in own]

    def get_lineage(self, run_id: str) -> list[Step]:
        """A run's full history: for forked runs, inherited ancestor steps followed by its own."""
        inherited = self._inherited_ids(run_id)
        by_id = {s.id: s for s in self._steps_by_ids(inherited)}
        return [by_id[i] for i in inherited if i in by_id] + self.get_steps(run_id)

    def count_lineage(self, run_id: str) -> tuple[int, int]:
        """``(inherited, own)`` step counts for a run."""
        own = int(
            self._query("SELECT COUNT(*) AS n FROM steps WHERE run_id = ?", (run_id,))[0]["n"]
        )
        return len(self._inherited_ids(run_id)), own

    def list_step_summaries(
        self, run_id: str, offset: int = 0, limit: int = 100
    ) -> tuple[list[StepSummary], int]:
        """One page of a run's history without bodies: ``(summaries, total)``.

        For forked runs the history starts with the inherited steps (``inherited=True``).
        """
        inherited = self._inherited_ids(run_id)
        own_total = int(
            self._query("SELECT COUNT(*) AS n FROM steps WHERE run_id = ?", (run_id,))[0]["n"]
        )
        total = len(inherited) + own_total
        page: list[StepSummary] = []
        cols = ", ".join(_SUMMARY_COLUMNS)
        if offset < len(inherited):
            wanted = inherited[offset : offset + limit]
            rows: dict[str, sqlite3.Row] = {}
            for i in range(0, len(wanted), 500):
                chunk = wanted[i : i + 500]
                marks = ",".join("?" for _ in chunk)
                for row in self._query(f"SELECT {cols} FROM steps WHERE id IN ({marks})", chunk):
                    rows[row["id"]] = row
            page = [_row_to_summary_step(rows[i], inherited=True) for i in wanted if i in rows]
        remaining = limit - len(page)
        if remaining > 0:
            own_offset = max(0, offset - len(inherited))
            page += [
                _row_to_summary_step(row, inherited=False)
                for row in self._query(
                    f"SELECT {cols} FROM steps WHERE run_id = ?"
                    " ORDER BY idx, started_at LIMIT ? OFFSET ?",
                    (run_id, remaining, own_offset),
                )
            ]
        return page, total


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


def _row_to_summary_step(row: sqlite3.Row, *, inherited: bool) -> StepSummary:
    error = loads(row["error"])
    results = {
        r.get("tool_use_id"): r
        for r in (loads(row["tool_results"], []) or [])
        if isinstance(r, dict)
    }
    calls = []
    for call in loads(row["tool_calls"], []) or []:
        if not isinstance(call, dict):
            continue
        result = results.get(call.get("id"))
        calls.append(
            {
                "id": call.get("id"),
                "name": call.get("name"),
                "input_preview": tool_input_preview(call.get("input")),
                "has_result": result is not None,
                "is_error": bool(result and result.get("is_error")),
            }
        )
    return StepSummary(
        id=row["id"],
        run_id=row["run_id"],
        idx=row["idx"],
        provider=row["provider"],
        started_at=row["started_at"],
        parent_step_id=row["parent_step_id"],
        kind=row["kind"],
        operation=row["operation"],
        model=row["model"],
        stop_reason=row["stop_reason"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        cache_read_tokens=row["cache_read_tokens"],
        cache_write_tokens=row["cache_write_tokens"],
        cost_usd=row["cost_usd"],
        latency_ms=row["latency_ms"],
        ttft_ms=row["ttft_ms"],
        streamed=bool(row["streamed"]),
        incomplete=bool(row["incomplete"]),
        is_replay=bool(row["is_replay"]),
        error_type=error.get("type") if isinstance(error, dict) else None,
        error_status=error.get("status_code") if isinstance(error, dict) else None,
        input_preview=row["input_preview"],
        output_preview=row["output_preview"],
        request_hash=row["request_hash"],
        response_hash=row["response_hash"],
        message_count=row["message_count"] or 0,
        tool_calls=calls,
        inherited=inherited,
    )


def fill_digest(step: Step) -> None:
    """Compute the derived preview/hash columns for a step about to be stored."""
    step.input_preview = input_preview(step.request)
    step.output_preview = output_preview(step.response, step.error)
    step.request_hash = hash_json(step.request)
    step.response_hash = hash_json(step.response)
    step.message_count = message_count(step.request)


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def encode_cursor(created_at: float, run_id: str) -> str:
    raw = f"{created_at!r}|{run_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[float, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        created, run_id = raw.split("|", 1)
        return float(created), run_id
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"invalid cursor {cursor!r}") from exc
