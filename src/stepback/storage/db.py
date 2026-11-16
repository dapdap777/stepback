"""SQLite connection handling and schema migrations."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

from .._util import loads
from . import digest

SCHEMA_VERSION = 2

Migration = str | Callable[[sqlite3.Connection], None]


def _v2_step_digests(conn: sqlite3.Connection) -> None:
    """Add preview/hash columns used by paginated step lists, and backfill them."""
    for column, ctype in (
        ("input_preview", "TEXT"),
        ("output_preview", "TEXT"),
        ("request_hash", "TEXT"),
        ("response_hash", "TEXT"),
        ("message_count", "INTEGER NOT NULL DEFAULT 0"),
    ):
        conn.execute(f"ALTER TABLE steps ADD COLUMN {column} {ctype}")
    last_rowid = 0
    while True:
        rows = conn.execute(
            "SELECT rowid, request, response, error FROM steps WHERE rowid > ?"
            " ORDER BY rowid LIMIT 500",
            (last_rowid,),
        ).fetchall()
        if not rows:
            break
        for row in rows:
            request = loads(row["request"], {}) or {}
            response = loads(row["response"])
            error = loads(row["error"])
            conn.execute(
                "UPDATE steps SET input_preview = ?, output_preview = ?, request_hash = ?,"
                " response_hash = ?, message_count = ? WHERE rowid = ?",
                (
                    digest.input_preview(request),
                    digest.output_preview(response, error),
                    digest.hash_json(request),
                    digest.hash_json(response),
                    digest.message_count(request),
                    row["rowid"],
                ),
            )
        last_rowid = rows[-1]["rowid"]


_MIGRATIONS: dict[int, Migration] = {
    1: """
    CREATE TABLE IF NOT EXISTS runs (
        id                  TEXT PRIMARY KEY,
        name                TEXT NOT NULL,
        status              TEXT NOT NULL DEFAULT 'running',
        created_at          REAL NOT NULL,
        ended_at            REAL,
        parent_run_id       TEXT REFERENCES runs(id) ON DELETE SET NULL,
        forked_from_step_id TEXT,
        metadata            TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_runs_parent ON runs(parent_run_id);

    CREATE TABLE IF NOT EXISTS steps (
        id                  TEXT PRIMARY KEY,
        run_id              TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        parent_step_id      TEXT,
        idx                 INTEGER NOT NULL,
        kind                TEXT NOT NULL DEFAULT 'llm',
        provider            TEXT NOT NULL,
        operation           TEXT,
        model               TEXT,
        request             TEXT NOT NULL,
        response            TEXT,
        tool_calls          TEXT NOT NULL DEFAULT '[]',
        tool_results        TEXT NOT NULL DEFAULT '[]',
        stop_reason         TEXT,
        input_tokens        INTEGER,
        output_tokens       INTEGER,
        cache_read_tokens   INTEGER,
        cache_write_tokens  INTEGER,
        cost_usd            REAL,
        latency_ms          REAL,
        ttft_ms             REAL,
        streamed            INTEGER NOT NULL DEFAULT 0,
        incomplete          INTEGER NOT NULL DEFAULT 0,
        error               TEXT,
        started_at          REAL NOT NULL,
        is_replay           INTEGER NOT NULL DEFAULT 0,
        replay_overrides    TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_steps_run ON steps(run_id, idx);
    CREATE INDEX IF NOT EXISTS idx_steps_parent ON steps(parent_step_id);
    """,
    2: _v2_step_digests,
}


def connect(path: Path) -> sqlite3.Connection:
    """Open (creating if needed) the trace database and bring its schema up to date."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_gitignore(path.parent)
    conn = sqlite3.connect(str(path), timeout=10.0, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # readers (UI/CLI) never block the recorder
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current >= SCHEMA_VERSION:
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Re-read inside the write lock: another process may have migrated meanwhile.
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        for version in range(current + 1, SCHEMA_VERSION + 1):
            migration = _MIGRATIONS[version]
            if isinstance(migration, str):
                for statement in migration.split(";"):
                    if statement.strip():
                        conn.execute(statement)
            else:
                migration(conn)
            conn.execute(f"PRAGMA user_version = {version}")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _write_gitignore(directory: Path) -> None:
    """Keep traces (which contain prompts) out of version control by default."""
    marker = directory / ".gitignore"
    if directory.name == ".stepback" and not marker.exists():
        try:
            marker.write_text("# Created by stepback: traces are local-only.\n*\n")
        except OSError:
            pass
