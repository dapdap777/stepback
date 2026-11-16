"""The recorder: hands finished calls to a background thread that writes them to SQLite.

Hot path (the user's thread): :meth:`Recorder.begin` and :meth:`CallHandle.finish`.
They do constant-time bookkeeping plus a ``queue.put``. Everything else (JSON conversion,
redaction, cost estimation, SQL) happens on a daemon writer thread. The queue is drained
by an ``atexit`` hook, so short scripts keep their last steps.
"""

from __future__ import annotations

import atexit
import contextvars
import functools
import inspect
import os
import queue
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import Any, TypeVar

from .. import config
from .._util import new_id, now, warn_once
from ..storage.models import Run, Step
from ..storage.store import TraceStore
from .base import CallHandle, NormalizedCall, ProviderAdapter, normalize_error
from .pricing import estimate_cost
from .redact import redact

F = TypeVar("F", bound=Callable[..., Any])


class RunContext:
    """In-memory state for one run (step counter)."""

    __slots__ = ("_counter", "_lock", "id", "name")

    def __init__(self, run_id: str, name: str) -> None:
        self.id = run_id
        self.name = name
        self._counter = 0
        self._lock = threading.Lock()

    def next_idx(self) -> int:
        with self._lock:
            idx = self._counter
            self._counter += 1
            return idx


_current_run: contextvars.ContextVar[RunContext | None] = contextvars.ContextVar(
    "stepback_current_run", default=None
)


class _RunState:
    """Writer-thread-only state used to link steps into a tree."""

    __slots__ = ("last_step_id", "tool_owner")

    def __init__(self) -> None:
        self.last_step_id: str | None = None
        self.tool_owner: dict[str, str] = {}


class Recorder:
    def __init__(self, db_path: str | os.PathLike[str] | None = None) -> None:
        self._explicit_db_path = db_path
        self._queue: queue.SimpleQueue[tuple[Any, ...]] = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        self._thread_lock = threading.Lock()
        self._pid = os.getpid()
        self._store: TraceStore | None = None
        self._store_failed = False
        self._run_states: dict[str, _RunState] = {}
        self._default_run: RunContext | None = None
        self._default_lock = threading.Lock()
        self._uncaught_exception = False
        self._closed = False
        atexit.register(self.shutdown)

    # ------------------------------------------------------------------ properties
    @property
    def db_path(self) -> Path:
        return config.resolve_db_path(self._explicit_db_path)

    # ------------------------------------------------------------------ runs
    def current_run(self) -> RunContext:
        run = _current_run.get()
        if run is not None:
            return run
        return self._get_default_run()

    def _get_default_run(self) -> RunContext:
        with self._default_lock:
            if self._default_run is None:
                name = config.settings.default_run_name or _script_name()
                run_id = new_id("run")
                self._default_run = RunContext(run_id, name)
                self._enqueue(
                    ("run", Run(id=run_id, name=name, created_at=now(), metadata=_run_metadata()))
                )
                self._install_excepthook()
            return self._default_run

    def start_run(self, name: str, metadata: dict[str, Any] | None = None) -> RunContext:
        run_id = new_id("run")
        ctx = RunContext(run_id, name)
        meta = _run_metadata()
        meta.update(metadata or {})
        self._enqueue(("run", Run(id=run_id, name=name, created_at=now(), metadata=meta)))
        return ctx

    def end_run(self, run: RunContext, status: str = "ok") -> None:
        self._enqueue(("end", run.id, status, now()))

    # ------------------------------------------------------------------ calls (hot path)
    def begin(
        self,
        adapter: ProviderAdapter,
        operation: str,
        request: dict[str, Any],
        streamed: bool = False,
    ) -> CallHandle | None:
        """Start tracking a call. Returns None (and records nothing) if disabled or on error."""
        if not config.settings.enabled or self._closed:
            return None
        try:
            run = self.current_run()
            return CallHandle(
                self, adapter, run, new_id("step"), run.next_idx(), operation, request, streamed
            )
        except Exception as exc:
            warn_once("begin", "failed to start recording a call", exc)
            return None

    def _submit_call(self, handle: CallHandle) -> None:
        self._enqueue(("call", handle))

    # ------------------------------------------------------------------ queue / writer
    def _enqueue(self, item: tuple[Any, ...]) -> None:
        try:
            self._ensure_writer()
            self._queue.put(item)
        except Exception as exc:
            warn_once("enqueue", "failed to queue a record", exc)

    def _ensure_writer(self) -> None:
        pid = os.getpid()
        if self._thread is not None and self._thread.is_alive() and pid == self._pid:
            return
        with self._thread_lock:
            if pid != self._pid:
                # Forked child: the parent's writer thread does not exist here.
                self._pid = pid
                self._queue = queue.SimpleQueue()
                self._store = None
                self._run_states = {}
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._writer_loop, name="stepback-writer", daemon=True
                )
                self._thread.start()

    def _get_store(self) -> TraceStore | None:
        if self._store is None and not self._store_failed:
            try:
                self._store = TraceStore(self.db_path)
            except Exception as exc:
                self._store_failed = True
                warn_once("store", f"cannot open trace database at {self.db_path}", exc)
        return self._store

    def _writer_loop(self) -> None:
        q = self._queue
        while True:
            item = q.get()
            kind = item[0]
            if kind == "flush":
                item[1].set()
                continue
            if kind == "stop":
                item[1].set()
                return
            try:
                store = self._get_store()
                if store is None:
                    continue
                if kind == "run":
                    store.create_run(item[1])
                elif kind == "end":
                    store.end_run(item[1], item[2], item[3])
                    self._run_states.pop(item[1], None)
                elif kind == "call":
                    self._write_call(store, item[1])
            except Exception as exc:
                warn_once(f"write-{kind}", f"failed to write {kind} record", exc)

    def _write_call(self, store: TraceStore, handle: CallHandle) -> None:
        try:
            normalized = handle.adapter.normalize(handle)
        except Exception as exc:
            warn_once("normalize", "failed to normalize a call; storing minimal record", exc)
            normalized = NormalizedCall(model=None, request={}, error=normalize_error(handle.error))
        if normalized.error is None and handle.error is not None:
            normalized.error = normalize_error(handle.error)

        state = self._run_states.setdefault(handle.run.id, _RunState())

        # Link tool results in this request back to the step whose tool calls they answer.
        results_by_owner: dict[str, list[dict[str, Any]]] = {}
        for result in normalized.tool_results_in_request:
            owner = state.tool_owner.get(str(result.get("tool_use_id")))
            if owner is not None:
                results_by_owner.setdefault(owner, []).append(result)
        parent_step_id = (
            next(reversed(list(results_by_owner)), None) if results_by_owner else None
        ) or state.last_step_id

        usage = normalized.usage or {}
        cache_write = usage.get("cache_creation_input_tokens")
        step = Step(
            id=handle.id,
            run_id=handle.run.id,
            parent_step_id=parent_step_id,
            idx=handle.idx,
            provider=handle.adapter.name,
            operation=handle.operation,
            model=normalized.model,
            request=redact(normalized.request),
            response=redact(normalized.response),
            tool_calls=redact(normalized.tool_calls),
            stop_reason=normalized.stop_reason,
            input_tokens=_int_or_none(usage.get("input_tokens")),
            output_tokens=_int_or_none(usage.get("output_tokens")),
            cache_read_tokens=_int_or_none(usage.get("cache_read_input_tokens")),
            cache_write_tokens=_int_or_none(cache_write),
            cost_usd=estimate_cost(normalized.model, usage),
            latency_ms=handle.latency_ms,
            ttft_ms=handle.ttft_ms,
            streamed=handle.streamed,
            incomplete=handle.incomplete,
            error=redact(normalized.error),
            started_at=handle.started_at,
        )
        store.insert_step(step)
        for owner, results in results_by_owner.items():
            store.add_tool_results(owner, redact(results))
            for result in results:
                state.tool_owner.pop(str(result.get("tool_use_id")), None)
        for call in normalized.tool_calls:
            if call.get("id"):
                state.tool_owner[str(call["id"])] = step.id
        state.last_step_id = step.id

    # ------------------------------------------------------------------ flushing
    def flush(self, timeout: float | None = None) -> bool:
        """Block until everything queued so far is written. Returns False on timeout."""
        thread = self._thread
        if thread is None or not thread.is_alive() or os.getpid() != self._pid:
            return True
        done = threading.Event()
        self._queue.put(("flush", done))
        return done.wait(config.settings.flush_timeout if timeout is None else timeout)

    def shutdown(self) -> None:
        """atexit hook: close the default run and drain the queue (bounded by flush_timeout)."""
        if self._closed:
            return
        try:
            if self._default_run is not None:
                status = "error" if self._uncaught_exception else "ok"
                self.end_run(self._default_run, status)
            self.flush()
        except Exception as exc:  # pragma: no cover - defensive
            warn_once("shutdown", "failed to flush traces at exit", exc)
        finally:
            self._closed = True

    def _install_excepthook(self) -> None:
        original = sys.excepthook

        def hook(*args: Any) -> None:
            self._uncaught_exception = True
            original(*args)

        try:
            sys.excepthook = hook
        except Exception:  # pragma: no cover
            pass


def _int_or_none(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) else None


def _script_name() -> str:
    argv0 = sys.argv[0] if sys.argv and sys.argv[0] else ""
    if argv0 in ("", "-c", "-m"):
        return "python"
    return Path(argv0).name or "python"


def _run_metadata() -> dict[str, Any]:
    return {
        "argv": list(sys.argv[:8]),
        "cwd": os.getcwd(),
        "pid": os.getpid(),
        "python": sys.version.split()[0],
    }


# ---------------------------------------------------------------------- global recorder
_recorder: Recorder | None = None
_recorder_lock = threading.Lock()


def get_recorder() -> Recorder:
    global _recorder
    if _recorder is None:
        with _recorder_lock:
            if _recorder is None:
                _recorder = Recorder()
    return _recorder


def _reset_recorder_for_tests() -> None:
    """Flush and drop the global recorder (tests only)."""
    global _recorder
    with _recorder_lock:
        if _recorder is not None:
            _recorder.shutdown()
            atexit.unregister(_recorder.shutdown)
        _recorder = None


# ---------------------------------------------------------------------- run scopes
class run:
    """Group the calls made inside a block (or function) into one named run.

    Works as a context manager (sync or async) and as a decorator::

        with stepback.run("triage", metadata={"ticket": 42}):
            agent.handle(ticket)

        @stepback.run("nightly-eval")
        async def main(): ...
    """

    def __init__(self, name: str | None = None, metadata: dict[str, Any] | None = None) -> None:
        self.name = name
        self.metadata = metadata
        self._ctx: RunContext | None = None
        self._token: contextvars.Token[RunContext | None] | None = None

    @property
    def id(self) -> str | None:
        return self._ctx.id if self._ctx else None

    def _enter(self) -> run:
        try:
            recorder = get_recorder()
            self._ctx = recorder.start_run(self.name or _script_name(), self.metadata)
            self._token = _current_run.set(self._ctx)
        except Exception as exc:
            warn_once("run-enter", "failed to start a run", exc)
        return self

    def _exit(self, exc_type: type[BaseException] | None) -> None:
        try:
            if self._token is not None:
                _current_run.reset(self._token)
            if self._ctx is not None:
                get_recorder().end_run(self._ctx, "error" if exc_type else "ok")
        except Exception as exc:
            warn_once("run-exit", "failed to end a run", exc)
        finally:
            self._ctx = None
            self._token = None

    def __enter__(self) -> run:
        return self._enter()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._exit(exc_type)

    async def __aenter__(self) -> run:
        return self._enter()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._exit(exc_type)

    def __call__(self, func: F) -> F:
        name = self.name or getattr(func, "__name__", None)
        metadata = self.metadata

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                async with run(name, metadata):
                    return await func(*args, **kwargs)

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with run(name, metadata):
                return func(*args, **kwargs)

        return wrapper  # type: ignore[return-value]
