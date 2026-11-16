"""Seed a trace database for the Playwright tests and README screenshots.

    uv run python frontend/e2e/seed.py /tmp/stepback-e2e.db

Writes deterministic content (timestamps are relative to "now"):

* ``repo-triage-agent``: 10 steps with parallel tool calls, a retried 529 error and a
  streamed final answer (the hero timeline).
* ``repo-triage-agent (replay #9)``: a fork of it with an edited system prompt.
* ``summarize-ticket``, ``research-agent`` (fails): small runs for the list.
* ``xss-probe``: hostile payloads in every field that the UI renders.
* ``long-run``: 3,000 steps for the virtualized timeline.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

from stepback.recorder.pricing import estimate_cost
from stepback.storage import Run, Step, TraceStore

MODEL = "claude-opus-5-5"
SYSTEM = (
    "You are a meticulous engineering assistant. Triage new GitHub issues: reproduce the bug, "
    "find the root cause in the code, and propose a minimal fix. Use tools for every fact."
)
TOOLS = [
    {
        "name": "search_issues",
        "description": "Search the issue tracker",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
    },
    {
        "name": "read_file",
        "description": "Read a file from the repository",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
    },
    {
        "name": "run_tests",
        "description": "Run a subset of the test suite",
        "input_schema": {"type": "object", "properties": {"target": {"type": "string"}}},
    },
    {
        "name": "grep",
        "description": "Search the codebase",
        "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}},
    },
]


class Builder:
    """Accumulates an Anthropic-style conversation and writes one step per call."""

    def __init__(self, store: TraceStore, run_id: str, t0: float, system: str = SYSTEM) -> None:
        self.store = store
        self.run_id = run_id
        self.t = t0
        self.idx = 0
        self.parent: str | None = None
        self.system = system
        self.messages: list[dict[str, Any]] = []
        self.pending_tools: dict[str, str] = {}

    def user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def tool_results(self, results: list[tuple[str, str, bool]], gap: float) -> None:
        """Results for the previous step's tool calls; ``gap`` = seconds the tools took."""
        self.t += gap
        blocks = [
            {"type": "tool_result", "tool_use_id": tid, "content": text, "is_error": err}
            for tid, text, err in results
        ]
        self.messages.append({"role": "user", "content": blocks})
        if self.parent:
            self.store.add_tool_results(
                self.parent,
                [{"tool_use_id": tid, "content": text, "is_error": err} for tid, text, err in results],
            )

    def call(
        self,
        content: list[dict[str, Any]],
        *,
        latency: float,
        in_tokens: int,
        out_tokens: int,
        stop: str | None = None,
        ttft: float | None = None,
        cache_read: int = 0,
        error: dict[str, Any] | None = None,
        step_id: str | None = None,
        replay_overrides: dict[str, Any] | None = None,
    ) -> str:
        request = {
            "model": MODEL,
            "max_tokens": 4096,
            "system": self.system,
            "tools": TOOLS,
            "messages": [dict(m) for m in self.messages],
        }
        if ttft is not None:
            request["stream"] = True
        usage = {
            "input_tokens": in_tokens,
            "output_tokens": out_tokens,
            "cache_read_input_tokens": cache_read,
        }
        stop = stop or ("tool_use" if any(b["type"] == "tool_use" for b in content) else "end_turn")
        response = None
        if error is None:
            response = {
                "id": f"msg_{self.run_id}_{self.idx}",
                "type": "message",
                "role": "assistant",
                "model": MODEL,
                "content": content,
                "stop_reason": stop,
                "usage": usage,
            }
        sid = step_id or f"step_{self.run_id[4:]}_{self.idx:04d}"
        self.store.insert_step(
            Step(
                id=sid,
                run_id=self.run_id,
                idx=self.idx,
                provider="anthropic",
                operation="messages.stream" if ttft is not None else "messages.create",
                model=MODEL,
                request=request,
                response=response,
                tool_calls=[
                    {"id": b["id"], "name": b["name"], "input": b["input"], "type": "tool_use"}
                    for b in content
                    if b["type"] == "tool_use"
                ],
                stop_reason=stop if error is None else None,
                input_tokens=None if error else in_tokens,
                output_tokens=None if error else out_tokens,
                cache_read_tokens=None if error else cache_read,
                cost_usd=None if error else estimate_cost(MODEL, usage),
                latency_ms=latency * 1000,
                ttft_ms=ttft * 1000 if ttft is not None else None,
                streamed=ttft is not None,
                error=error,
                started_at=self.t,
                parent_step_id=self.parent,
                is_replay=replay_overrides is not None,
                replay_overrides=replay_overrides,
            )
        )
        self.t += latency
        self.idx += 1
        if error is None:
            self.parent = sid
            self.messages.append({"role": "assistant", "content": content})
        return sid


def text(t: str) -> dict[str, Any]:
    return {"type": "text", "text": t}


def tool(tid: str, name: str, **inp: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def thinking() -> dict[str, Any]:
    return {"type": "thinking", "thinking": "", "signature": "sig"}


def seed_triage(store: TraceStore, now: float) -> tuple[str, str]:
    run_id = "run_7f3a9c2e41d0"
    t0 = now - 600
    store.create_run(Run(id=run_id, name="repo-triage-agent", created_at=t0, metadata={"issue": 4411}))
    b = Builder(store, run_id, t0 + 0.05)
    b.user(
        "New issue #4411: \"Exporting a report with more than 1,000 rows silently drops the "
        "last page.\" Please triage it."
    )
    b.call(
        [thinking(), text("I'll look for related issues and the export code."),
         tool("toolu_01", "search_issues", query="export drops rows"),
         tool("toolu_02", "grep", pattern="def export_report")],
        latency=2.41, in_tokens=1842, out_tokens=164)
    b.tool_results([("toolu_01", "#3980 (closed): CSV export truncates at 1000 rows (fixed in v2.3)", False),
                    ("toolu_02", "src/reports/export.py:88: def export_report(query, fmt='csv'):", False)], gap=0.62)
    b.call([text("There was a similar bug before. Let me read the export module."),
            tool("toolu_03", "read_file", path="src/reports/export.py")],
           latency=1.38, in_tokens=2120, out_tokens=71, cache_read=1700)
    b.tool_results([("toolu_03", "def export_report(query, fmt='csv'):\n    pages = paginate(query, page_size=1000)\n    rows = []\n    for page in pages[:-1]:\n        rows.extend(page)\n    return render(rows, fmt)", False)], gap=0.11)
    b.call([thinking(), text("`pages[:-1]` skips the final page. I'll check `paginate` to confirm."),
            tool("toolu_04", "read_file", path="src/reports/paginate.py")],
           latency=3.12, in_tokens=2540, out_tokens=118, cache_read=2100)
    b.tool_results([("toolu_04", "def paginate(query, page_size):\n    return [query[i:i + page_size] for i in range(0, len(query), page_size)]", False)], gap=0.09)
    b.call([text("Confirmed. Let me reproduce with the existing tests."),
            tool("toolu_05", "run_tests", target="tests/reports/test_export.py")],
           latency=1.06, in_tokens=2760, out_tokens=52, cache_read=2500)
    b.tool_results([("toolu_05", "4 passed in 1.84s", False)], gap=4.8)
    b.call([text("The tests pass because none exports more than one page. I'll search for the regression."),
            tool("toolu_06", "grep", pattern="pages\\[:-1\\]"),
            tool("toolu_07", "search_issues", query="pagination refactor")],
           latency=1.94, in_tokens=2890, out_tokens=97, cache_read=2700)
    b.tool_results([("toolu_06", "src/reports/export.py:91:    for page in pages[:-1]:", False),
                    ("toolu_07", "#4302 (merged): Refactor pagination to support streaming exports", False)], gap=0.71)
    b.call([text("The refactor in #4302 introduced it. Let me write a failing test first."),
            tool("toolu_08", "run_tests", target="tests/reports/test_export.py::test_multi_page")],
           latency=2.67, in_tokens=3105, out_tokens=211, cache_read=2900)
    b.tool_results([("toolu_08", "FAILED test_multi_page - AssertionError: 1000 != 1500", True)], gap=3.9)
    # A transient overload, retried by the SDK user code.
    b.call([], latency=0.38, in_tokens=0, out_tokens=0, error={
        "type": "OverloadedError", "status_code": 529, "message": "Error code: 529",
        "body": {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}})
    b.t += 1.2
    b.call([text("The new test reproduces it: 1000 of 1500 rows. Here's the fix."),
            tool("toolu_09", "read_file", path="src/reports/render.py")],
           latency=1.71, in_tokens=3390, out_tokens=88, cache_read=3100)
    b.tool_results([("toolu_09", "def render(rows, fmt): ...", False)], gap=0.08)
    b.call([text("Applying the change and re-running the export tests."),
            tool("toolu_10", "run_tests", target="tests/reports")],
           latency=1.22, in_tokens=3520, out_tokens=143, cache_read=3300)
    b.tool_results([("toolu_10", "12 passed in 3.02s", False)], gap=5.4)
    fork_parent_messages = [dict(m) for m in b.messages]
    fork_parent = b.parent
    replayed = b.call(
        [text(
            "## Triage: #4411\n\n**Root cause:** `export_report` iterates `pages[:-1]`, so the final "
            "page is dropped whenever results span more than one page. Regression from #4302.\n\n"
            "**Fix:** iterate over all pages:\n\n```python\nfor page in pages:\n    rows.extend(page)\n```\n\n"
            "**Tests:** added `test_multi_page` (fails before, passes after). All 12 report tests pass.\n\n"
            "| Severity | Affected | Since |\n|---|---|---|\n| High (silent data loss) | exports > 1,000 rows | v2.6.0 |"
        )],
        latency=4.86, in_tokens=3810, out_tokens=342, cache_read=3500, ttft=0.71)
    store.end_run(run_id, "ok", b.t + 0.05)

    # Fork: replay the final step with a terser system prompt.
    fork_id = "run_c41b8e09a7f2"
    store.create_run(Run(id=fork_id, name="repo-triage-agent (replay #9)", created_at=now - 60,
                         parent_run_id=run_id, forked_from_step_id=replayed))
    fb = Builder(store, fork_id, now - 59.5, system="You are a terse engineering assistant. Reply in at most three sentences.")
    fb.messages = fork_parent_messages
    fb.parent = fork_parent
    fb.idx = 9
    fb.call([text("Root cause: `export_report` loops over `pages[:-1]`, dropping the last page (regression from #4302). Fix: iterate all pages. Added `test_multi_page`; all report tests pass.")],
            latency=1.93, in_tokens=3790, out_tokens=61, cache_read=3500,
            replay_overrides={"system": fb.system})
    store.end_run(fork_id, "ok", now - 57)
    return run_id, fork_id


def seed_small_runs(store: TraceStore, now: float) -> None:
    run = "run_2d9e61b0c5a8"
    store.create_run(Run(id=run, name="summarize-ticket", created_at=now - 1500))
    b = Builder(store, run, now - 1499.9, system="Summarize support tickets in one sentence.")
    b.user("Ticket #8812: Customer was charged twice for invoice #4411 and asks for a refund of the duplicate charge.")
    b.call([text("The customer was double-charged for invoice #4411 and requests a refund of the duplicate.")],
           latency=0.84, in_tokens=412, out_tokens=24)
    store.end_run(run, "ok", b.t + 0.02)

    run = "run_9a0f3e7d2b16"
    store.create_run(Run(id=run, name="research-agent", created_at=now - 3600 * 3))
    b = Builder(store, run, now - 3600 * 3 + 0.1)
    b.user("Compare three vector databases for a 50M-document corpus.")
    b.call([text("I'll start by searching recent benchmarks."), tool("toolu_r1", "search_issues", query="vector db benchmark")],
           latency=3.4, in_tokens=1210, out_tokens=96)
    b.tool_results([("toolu_r1", "rate limited by upstream", True)], gap=0.4)
    b.call([], latency=0.21, in_tokens=0, out_tokens=0, error={
        "type": "RateLimitError", "status_code": 429, "message": "Error code: 429",
        "body": {"type": "error", "error": {"type": "rate_limit_error", "message": "Number of request tokens has exceeded your per-minute rate limit"}}})
    store.end_run(run, "error", b.t + 0.1)


XSS = [
    "<script>window.__stepback_pwned = 'script'</script>",
    '<img src="https://tracker.invalid/pixel.png" onerror="window.__stepback_pwned=\'img\'">',
    '<iframe src="javascript:window.__stepback_pwned=\'iframe\'"></iframe>',
    "[click me](javascript:window.__stepback_pwned='link')",
    "![tracking pixel](https://tracker.invalid/beacon.gif)",
    "<svg onload=\"window.__stepback_pwned='svg'\"></svg>",
]


def seed_xss(store: TraceStore, now: float) -> None:
    run = "run_xss000000001"
    store.create_run(Run(id=run, name="xss-probe <script>alert(1)</script>", created_at=now - 900))
    b = Builder(store, run, now - 899.9, system="<b>system</b> " + XSS[0])
    b.user("Payloads:\n" + "\n".join(XSS))
    b.call([text("Rendering markdown with payloads:\n\n" + "\n\n".join(XSS)),
            tool("toolu_x1", "read_file", path="<script>window.__stepback_pwned='tool'</script>")],
           latency=1.0, in_tokens=100, out_tokens=50)
    b.tool_results([("toolu_x1", XSS[1] + " " + XSS[0], False)], gap=0.1)
    b.call([text("Done. " + XSS[3])], latency=0.5, in_tokens=120, out_tokens=10)
    store.end_run(run, "ok", b.t)


def seed_long(store: TraceStore, now: float, n: int = 3000) -> None:
    run = "run_10ng000000003k"
    store.create_run(Run(id=run, name="long-run", created_at=now - 7200))
    t = now - 7199.0
    parent = None
    with store._lock:
        conn = store._conn
        conn.execute("BEGIN")
        try:
            for i in range(n):
                sid = f"step_long_{i:05d}"
                is_err = i % 397 == 396
                step = Step(
                    id=sid,
                    run_id=run,
                    idx=i,
                    provider="anthropic",
                    operation="messages.create",
                    model=MODEL,
                    request={"model": MODEL, "max_tokens": 1024, "messages": [
                        {"role": "user", "content": f"Process batch item {i}"}]},
                    response=None if is_err else {"content": [
                        {"type": "text", "text": f"Processed item {i}."},
                        {"type": "tool_use", "id": f"toolu_l{i}", "name": "run_tests", "input": {"target": f"item_{i}"}},
                    ], "stop_reason": "tool_use"},
                    tool_calls=[] if is_err else [{"id": f"toolu_l{i}", "name": "run_tests", "input": {"target": f"item_{i}"}}],
                    stop_reason=None if is_err else "tool_use",
                    input_tokens=None if is_err else 900 + i % 300,
                    output_tokens=None if is_err else 40 + i % 60,
                    cost_usd=None if is_err else (900 + i % 300) * 4e-6 + (40 + i % 60) * 20e-6,
                    latency_ms=600 + (i * 37) % 1400,
                    error={"type": "InternalServerError", "status_code": 500, "message": "boom"} if is_err else None,
                    started_at=t,
                    parent_step_id=parent,
                )
                store.insert_step(step)
                t += step.latency_ms / 1000 + 0.6  # type: ignore[operator]
                parent = sid
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    store.end_run(run, "ok", t)


def main(path: str) -> None:
    db = Path(path)
    for suffix in ("", "-wal", "-shm"):
        Path(f"{db}{suffix}").unlink(missing_ok=True)
    now = time.time()
    with TraceStore(db) as store:
        seed_long(store, now)
        seed_small_runs(store, now)
        seed_xss(store, now)
        seed_triage(store, now)
    print(f"seeded {db}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".stepback/e2e.db")
