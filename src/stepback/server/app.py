"""FastAPI application behind ``stepback ui``."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import asdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.routing import APIRoute

from .. import __version__, replay
from ..replay import InvalidReplayRequest, ReplayError, SdkMissing, StepNotFound
from ..storage import RunHasForks, TraceStore
from ..storage.models import StepSummary
from .schemas import (
    DiffPair,
    DiffTotals,
    Health,
    ReplayBody,
    ReplayEstimate,
    ReplayOut,
    ReplayStatus,
    RunDetail,
    RunDiff,
    RunOut,
    RunPage,
    RunRef,
    StepDetail,
    StepPage,
    StepRef,
    StepSummaryOut,
)
from .security import LOOPBACK_HOSTS, LocalOnlyMiddleware, validate_allowlists

STATIC_DIR = Path(__file__).parent / "static"
MAX_RUN_PAGE = 200
MAX_STEP_PAGE = 500


def _operation_id(route: APIRoute) -> str:
    # Stable, readable operation ids for generated frontend clients (e.g. "listRuns").
    head, *rest = route.name.split("_")
    return head + "".join(part.title() for part in rest)


def create_app(
    db_path: str | os.PathLike[str] | None = None,
    *,
    allowed_hosts: Iterable[str] = LOOPBACK_HOSTS,
    allowed_origins: Iterable[str] = (),
    static_dir: Path | None = None,
    replay_client_factory: replay.ClientFactory | None = None,
    replay_mock: bool = False,
) -> FastAPI:
    """Build the app. ``allowed_hosts``/``allowed_origins`` feed :class:`LocalOnlyMiddleware`.

    ``replay_client_factory`` builds the Anthropic client for replays; by default a fresh
    client reads credentials from the environment at replay time (tests inject a fake).
    ``replay_mock`` sends replays to the bundled offline mock API instead (``ui --mock``).
    """
    if replay_mock and replay_client_factory is None:
        from ..demo.mock_api import mock_client

        replay_client_factory = mock_client
    allowed_hosts, allowed_origins = validate_allowlists(allowed_hosts, allowed_origins)
    store = TraceStore(db_path)
    static = static_dir or STATIC_DIR

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        store.close()

    app = FastAPI(
        lifespan=lifespan,
        title="Stepback",
        version=__version__,
        summary="Local API for the Stepback time-travel debugger.",
        openapi_url="/api/openapi.json",
        # Swagger UI / ReDoc pages load assets from a CDN; everything here stays local.
        docs_url=None,
        redoc_url=None,
        generate_unique_id_function=_operation_id,
    )
    app.state.store = store
    app.add_middleware(
        LocalOnlyMiddleware,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )

    # ------------------------------------------------------------------ helpers
    def run_or_404(run_id: str) -> RunOut:
        summary = store.summarize(run_id)
        if summary is None:
            raise HTTPException(404, f"run {run_id!r} not found")
        return RunOut.from_summary(summary)

    # ------------------------------------------------------------------ meta
    @app.get("/api/health", tags=["meta"])
    def get_health() -> Health:
        return Health(version=__version__, db_path=str(store.path), run_count=store.count_runs())

    # ------------------------------------------------------------------ runs
    @app.get("/api/runs", tags=["runs"])
    def list_runs(
        limit: Annotated[int, Query(ge=1, le=MAX_RUN_PAGE)] = 50,
        cursor: Annotated[str | None, Query(description="`next_cursor` of the last page")] = None,
        q: Annotated[str | None, Query(max_length=200, description="Name or id substring")] = None,
        status: Annotated[str | None, Query(pattern="^(running|ok|error)$")] = None,
        include_forks: bool = True,
    ) -> RunPage:
        try:
            summaries, next_cursor = store.page_runs(
                limit, cursor, include_forks=include_forks, query=q, status=status
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        total = store.count_runs_matching(include_forks=include_forks, query=q, status=status)
        return RunPage(
            items=[RunOut.from_summary(s) for s in summaries],
            total=total,
            next_cursor=next_cursor,
        )

    @app.get("/api/runs/{run_id}", tags=["runs"])
    def get_run(run_id: str) -> RunDetail:
        run = run_or_404(run_id)
        parent = store.get_run(run.parent_run_id) if run.parent_run_id else None
        fork_step = store.get_step(run.forked_from_step_id) if run.forked_from_step_id else None
        forks = [s for s in (store.summarize(f.id) for f in store.list_forks(run_id)) if s]
        inherited, own = store.count_lineage(run_id)
        return RunDetail(
            run=run,
            parent=RunRef.from_run(parent) if parent else None,
            forked_from_step=(
                StepRef(id=fork_step.id, run_id=fork_step.run_id, idx=fork_step.idx)
                if fork_step
                else None
            ),
            forks=[RunOut.from_summary(f) for f in forks],
            inherited_steps=inherited,
            own_steps=own,
        )

    @app.delete("/api/runs/{run_id}", status_code=204, tags=["runs"])
    def delete_run(
        run_id: str,
        cascade: Annotated[
            bool, Query(description="Also delete runs forked from this one.")
        ] = False,
    ) -> Response:
        run_or_404(run_id)
        try:
            store.delete_run(run_id, cascade_forks=cascade)
        except RunHasForks as exc:
            raise HTTPException(409, f"{exc}. Pass cascade=true to delete them as well.") from None
        return Response(status_code=204)

    # ------------------------------------------------------------------ steps
    @app.get("/api/runs/{run_id}/steps", tags=["steps"])
    def list_steps(
        run_id: str,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=MAX_STEP_PAGE)] = 100,
    ) -> StepPage:
        """A page of the run's history, without message bodies (see `getStep` for those).

        For forked runs the history begins with steps inherited from the parent run.
        """
        run_or_404(run_id)
        items, total = store.list_step_summaries(run_id, offset, limit)
        return StepPage(
            items=[StepSummaryOut.from_summary(s) for s in items],
            total=total,
            offset=offset,
            limit=limit,
        )

    @app.get("/api/steps/{step_id}", tags=["steps"])
    def get_step(step_id: str) -> StepDetail:
        step = store.get_step(step_id)
        if step is None:
            raise HTTPException(404, f"step {step_id!r} not found")
        parent = store.get_step(step.parent_step_id) if step.parent_step_id else None
        return StepDetail.from_step(step, parent.message_count if parent else None)

    # ------------------------------------------------------------------ diff
    @app.get("/api/diff", tags=["runs"])
    def diff_runs(
        a: Annotated[str, Query(description="Run id (left side)")],
        b: Annotated[str, Query(description="Run id (right side)")],
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=MAX_STEP_PAGE)] = 100,
    ) -> RunDiff:
        """Align two runs' histories step by step and flag differences.

        Steps are matched by request content (so a fork's inherited steps and identical
        re-runs line up even when one side has extra steps, e.g. a failed attempt); the
        unmatched stretches in between are paired in order.
        """
        run_a, run_b = run_or_404(a), run_or_404(b)
        steps_a = _all_summaries(store, a)
        steps_b = _all_summaries(store, b)
        pairs = [_pair(i, sa, sb) for i, (sa, sb) in enumerate(align_steps(steps_a, steps_b))]
        first = next((p.position for p in pairs if p.status not in ("shared", "same")), None)
        return RunDiff(
            a=run_a,
            b=run_b,
            totals_a=_totals(steps_a),
            totals_b=_totals(steps_b),
            pairs=pairs[offset : offset + limit],
            total_pairs=len(pairs),
            offset=offset,
            limit=limit,
            first_divergence=first,
        )

    # ------------------------------------------------------------------ replay
    def replay_error(exc: ReplayError) -> HTTPException:
        if isinstance(exc, StepNotFound):
            return HTTPException(404, str(exc))
        if isinstance(exc, InvalidReplayRequest):
            return HTTPException(422, {"code": exc.code, "message": str(exc), "errors": exc.errors})
        status = 500 if isinstance(exc, SdkMissing) else 400
        return HTTPException(status, {"code": exc.code, "message": str(exc)})

    @app.get("/api/replay/status", tags=["replay"])
    def get_replay_status() -> ReplayStatus:
        """Whether replay can authenticate. Reports the credential's source, never its value."""
        if replay_mock:
            return ReplayStatus(
                sdk_installed=True,
                sdk_version=None,
                credentials_configured=True,
                credential_source="offline mock API",
                message=None,
                known_models=replay.known_models(),
                mock=True,
            )
        status = replay.credential_status()
        return ReplayStatus(
            sdk_installed=status.sdk_installed,
            sdk_version=status.sdk_version,
            credentials_configured=status.configured,
            credential_source=status.source,
            message=status.message,
            known_models=replay.known_models(),
        )

    @app.post("/api/steps/{step_id}/replay/estimate", tags=["replay"])
    def estimate_replay(step_id: str, body: ReplayBody) -> ReplayEstimate:
        """Validate an edited request and estimate its input tokens and cost.

        Free: uses the token-counting endpoint (or a local estimate). Nothing is generated.
        """
        try:
            result = replay.estimate(
                store, step_id, body.request, client_factory=replay_client_factory
            )
        except ReplayError as exc:
            raise replay_error(exc) from None
        return ReplayEstimate(**asdict(result))

    @app.post("/api/steps/{step_id}/replay", status_code=201, tags=["replay"])
    def replay_step(step_id: str, body: ReplayBody) -> ReplayOut:
        """Send one (edited) LLM call and save the result as a new forked run. Costs money.

        API failures (auth, rate limit, invalid request...) are saved as a failed step in
        the fork and returned with status "error". The source run is never modified.
        """
        if not body.confirm:
            raise HTTPException(
                400,
                {
                    "code": "confirmation_required",
                    "message": "Replays are paid API calls: send confirm=true to proceed.",
                },
            )
        try:
            result = replay.replay_step(
                store, step_id, body.request, client_factory=replay_client_factory
            )
        except ReplayError as exc:
            raise replay_error(exc) from None
        return ReplayOut(**asdict(result))

    # ------------------------------------------------------------------ SPA
    @app.get("/api", include_in_schema=False)
    @app.get("/api/{rest:path}", include_in_schema=False)
    def api_not_found(rest: str = "") -> Response:
        raise HTTPException(404, "unknown API endpoint")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str, request: Request) -> Response:
        index = static / "index.html"
        if path:
            candidate = (static / path).resolve()
            if candidate.is_file() and static.resolve() in candidate.parents:
                return FileResponse(candidate)
        if index.is_file():
            return FileResponse(index, headers={"cache-control": "no-cache"})
        return HTMLResponse(_NOT_BUILT_PAGE, status_code=200)

    return app


def _all_summaries(store: TraceStore, run_id: str) -> list[StepSummary]:
    items: list[StepSummary] = []
    offset = 0
    while True:
        page, total = store.list_step_summaries(run_id, offset, 1000)
        items += page
        offset += len(page)
        if not page or offset >= total:
            return items


def align_steps(
    a: list[StepSummary], b: list[StepSummary]
) -> list[tuple[StepSummary | None, StepSummary | None]]:
    """Pair up two step histories, anchoring on steps with identical requests."""

    # Steps present in both histories (a fork's inherited steps) anchor on identity; others
    # on request content. Identity first matters when a failed call is retried with the very
    # same request: the shared retry must not be matched to the failed attempt.
    shared = {s.id for s in a} & {s.id for s in b}

    def key(step: StepSummary) -> str:
        if step.id in shared or not step.request_hash:
            return f"id:{step.id}"
        return f"rq:{step.request_hash}"

    matcher = SequenceMatcher(None, [key(s) for s in a], [key(s) for s in b], autojunk=False)
    pairs: list[tuple[StepSummary | None, StepSummary | None]] = []
    for _tag, i1, i2, j1, j2 in matcher.get_opcodes():
        left, right = a[i1:i2], b[j1:j2]
        for k in range(max(len(left), len(right))):
            pairs.append((left[k] if k < len(left) else None, right[k] if k < len(right) else None))
    return pairs


def _pair(position: int, a: StepSummary | None, b: StepSummary | None) -> DiffPair:
    if a is None or b is None:
        status = "added" if a is None else "removed"
        req_changed = resp_changed = True
    elif a.id == b.id:
        status, req_changed, resp_changed = "shared", False, False
    else:
        req_changed = a.request_hash != b.request_hash
        resp_changed = a.response_hash != b.response_hash
        status = "changed" if req_changed or resp_changed else "same"
    return DiffPair(
        position=position,
        status=status,  # type: ignore[arg-type]
        request_changed=req_changed,
        response_changed=resp_changed,
        a=StepSummaryOut.from_summary(a) if a else None,
        b=StepSummaryOut.from_summary(b) if b else None,
    )


def _totals(steps: list[StepSummary]) -> DiffTotals:
    costs = [s.cost_usd for s in steps if s.cost_usd is not None]
    return DiffTotals(
        steps=len(steps),
        input_tokens=sum(s.input_tokens or 0 for s in steps),
        output_tokens=sum(s.output_tokens or 0 for s in steps),
        cost_usd=sum(costs) if costs else None,
        latency_ms=sum(s.latency_ms or 0 for s in steps),
    )


_NOT_BUILT_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Stepback</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
</head><body>
<h1>Stepback</h1>
<p>The API is running, but the web UI hasn't been built in this checkout.</p>
<p>Build it with <code>cd frontend &amp;&amp; npm ci &amp;&amp; npm run build</code>, then reload.
Released packages from PyPI include the built UI.</p>
<p>Meanwhile: <code>stepback ls</code> and <code>stepback show</code> work in the terminal,
and the API schema is at <a href="/api/openapi.json">/api/openapi.json</a>.</p>
</body></html>"""


def openapi_schema() -> dict[str, Any]:
    """The OpenAPI document, without needing a database (used by `stepback openapi`)."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(Path(tmp) / "schema.db")
        schema = app.openapi()
        app.state.store.close()
        return schema
