"""Provider-neutral recording primitives and the adapter interface.

An adapter teaches Stepline how to hook one SDK (Anthropic today; OpenAI and others later).
It has two jobs:

1. ``install``: patch the client so each LLM call goes through
   :meth:`Recorder.begin` / :meth:`CallHandle.finish`. This runs on the user's thread
   and must stay cheap and never raise.
2. ``normalize``: turn the raw objects captured in a :class:`CallHandle` into a
   :class:`NormalizedCall`. This runs on the background writer thread, so it may do the
   heavier JSON conversion work.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, runtime_checkable

from .._util import to_jsonable, warn_once

if TYPE_CHECKING:
    from .recorder import Recorder, RunContext

ClientT = TypeVar("ClientT")


@dataclass
class NormalizedCall:
    """Everything Stepline stores about one LLM call, in plain JSON-able form."""

    model: str | None
    request: dict[str, Any]
    response: dict[str, Any] | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    # Tool results *sent in this request*: used to link them back to the step that asked.
    tool_results_in_request: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] | None = None
    stop_reason: str | None = None
    error: dict[str, Any] | None = None


@runtime_checkable
class ProviderAdapter(Protocol):
    """Interface each SDK integration implements."""

    #: Short provider name stored on each step, e.g. ``"anthropic"``.
    name: str

    def matches(self, client: object) -> bool:
        """Return True if this adapter knows how to wrap ``client``."""
        ...

    def install(self, client: ClientT, recorder: Recorder) -> ClientT:
        """Patch ``client`` in place so its LLM calls are recorded; return it."""
        ...

    def normalize(self, call: CallHandle) -> NormalizedCall:
        """Convert a finished call into a :class:`NormalizedCall` (runs off the hot path)."""
        ...


def normalize_error(error: BaseException | None) -> dict[str, Any] | None:
    if error is None:
        return None
    data: dict[str, Any] = {"type": type(error).__name__, "message": str(error)}
    status = getattr(error, "status_code", None)
    if isinstance(status, int):
        data["status_code"] = status
    body = getattr(error, "body", None)
    if body is not None:
        data["body"] = to_jsonable(body)
    return data


class CallHandle:
    """Tracks one in-flight LLM call. Created by :meth:`Recorder.begin`.

    All public methods are safe to call from user code paths: they never raise.
    """

    __slots__ = (
        "_done",
        "_lock",
        "_recorder",
        "_t0",
        "adapter",
        "error",
        "events",
        "id",
        "idx",
        "incomplete",
        "latency_ms",
        "operation",
        "request",
        "response",
        "run",
        "started_at",
        "streamed",
        "ttft_ms",
    )

    def __init__(
        self,
        recorder: Recorder,
        adapter: ProviderAdapter,
        run: RunContext,
        step_id: str,
        idx: int,
        operation: str,
        request: dict[str, Any],
        streamed: bool,
    ) -> None:
        self._recorder = recorder
        self.adapter = adapter
        self.run = run
        self.id = step_id
        self.idx = idx
        self.operation = operation
        self.request = request
        self.streamed = streamed
        self.started_at = time.time()
        self._t0 = time.perf_counter()
        self.ttft_ms: float | None = None
        self.latency_ms: float | None = None
        self.events: list[Any] = []
        self.response: Any = None
        self.error: BaseException | None = None
        self.incomplete = False
        self._done = False
        self._lock = threading.Lock()

    def restart_clock(self) -> None:
        """Reset timing, for APIs where the request is sent later than the call is created."""
        self.started_at = time.time()
        self._t0 = time.perf_counter()

    def on_event(self, event: Any, is_token: bool) -> None:
        """Record one streamed event. Called once per chunk, so it does almost nothing."""
        if is_token and self.ttft_ms is None:
            self.ttft_ms = (time.perf_counter() - self._t0) * 1000.0
        self.events.append(event)

    @property
    def done(self) -> bool:
        return self._done

    def finish(
        self,
        response: Any = None,
        error: BaseException | None = None,
        incomplete: bool = False,
    ) -> None:
        """Mark the call finished and hand it to the background writer. Idempotent."""
        try:
            with self._lock:
                if self._done:
                    return
                self._done = True
            self.latency_ms = (time.perf_counter() - self._t0) * 1000.0
            self.response = response
            self.error = error
            self.incomplete = incomplete
            self._recorder._submit_call(self)
        except Exception as exc:  # pragma: no cover - defensive
            warn_once("finish", "failed to finalize a recorded call", exc)
