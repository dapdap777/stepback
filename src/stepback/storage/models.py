"""Plain data records stored in SQLite."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Run:
    id: str
    name: str
    created_at: float
    status: str = "running"  # running | ok | error
    ended_at: float | None = None
    parent_run_id: str | None = None
    forked_from_step_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Step:
    id: str
    run_id: str
    idx: int
    provider: str
    request: dict[str, Any]
    started_at: float
    parent_step_id: str | None = None
    kind: str = "llm"
    operation: str | None = None
    model: str | None = None
    response: dict[str, Any] | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    # Results returned for this step's tool calls (filled in when the next request carries them).
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cost_usd: float | None = None
    latency_ms: float | None = None
    ttft_ms: float | None = None
    streamed: bool = False
    incomplete: bool = False
    error: dict[str, Any] | None = None
    is_replay: bool = False
    replay_overrides: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def total_tokens(self) -> int:
        return sum(
            t or 0
            for t in (
                self.input_tokens,
                self.output_tokens,
                self.cache_read_tokens,
                self.cache_write_tokens,
            )
        )


@dataclass
class RunSummary:
    """A run plus aggregates, as shown in run lists."""

    run: Run
    step_count: int = 0
    error_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    latency_ms: float = 0.0
    models: list[str] = field(default_factory=list)
    fork_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        data = self.run.to_dict()
        data.update(
            step_count=self.step_count,
            error_count=self.error_count,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cost_usd=self.cost_usd,
            latency_ms=self.latency_ms,
            models=self.models,
            fork_count=self.fork_count,
        )
        return data
