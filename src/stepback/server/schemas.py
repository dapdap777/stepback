"""Response models. These define the OpenAPI schema the frontend's types are generated from."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from ..storage.models import Run, RunSummary, Step, StepSummary


class Health(BaseModel):
    version: str
    db_path: str
    run_count: int


class RunOut(BaseModel):
    id: str
    name: str
    status: str
    created_at: float
    ended_at: float | None
    parent_run_id: str | None
    forked_from_step_id: str | None
    metadata: dict[str, Any]
    step_count: int
    error_count: int
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    latency_ms: float
    models: list[str]
    fork_count: int

    @classmethod
    def from_summary(cls, summary: RunSummary) -> RunOut:
        return cls(**summary.to_dict())


class RunPage(BaseModel):
    items: list[RunOut]
    total: int = Field(description="Runs matching the filters, across all pages.")
    next_cursor: str | None = Field(description="Pass as `cursor` for the next page.")


class RunRef(BaseModel):
    id: str
    name: str

    @classmethod
    def from_run(cls, run: Run) -> RunRef:
        return cls(id=run.id, name=run.name)


class StepRef(BaseModel):
    id: str
    run_id: str
    idx: int


class RunDetail(BaseModel):
    run: RunOut
    parent: RunRef | None = Field(description="The run this one was forked from.")
    forked_from_step: StepRef | None = Field(description="The step that was replayed.")
    forks: list[RunOut]
    inherited_steps: int = Field(description="Steps inherited from ancestor runs.")
    own_steps: int


class ToolCallSummary(BaseModel):
    id: str | None
    name: str | None
    input_preview: str
    has_result: bool
    is_error: bool


class StepSummaryOut(BaseModel):
    id: str
    run_id: str
    idx: int
    parent_step_id: str | None
    provider: str
    kind: str
    operation: str | None
    model: str | None
    stop_reason: str | None
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    cost_usd: float | None
    latency_ms: float | None
    ttft_ms: float | None
    streamed: bool
    incomplete: bool
    is_replay: bool
    started_at: float
    error_type: str | None
    error_status: int | None
    input_preview: str | None
    output_preview: str | None
    request_hash: str | None
    response_hash: str | None
    message_count: int
    tool_calls: list[ToolCallSummary]
    inherited: bool

    @classmethod
    def from_summary(cls, summary: StepSummary) -> StepSummaryOut:
        return cls(**summary.to_dict())


class StepPage(BaseModel):
    items: list[StepSummaryOut]
    total: int
    offset: int
    limit: int


class ToolCall(BaseModel):
    id: str | None = None
    name: str | None = None
    input: Any = None
    type: str | None = None


class ToolResult(BaseModel):
    tool_use_id: str | None = None
    content: Any = None
    is_error: bool = False


class StepDetail(BaseModel):
    """A step with full request/response bodies."""

    id: str
    run_id: str
    idx: int
    parent_step_id: str | None
    provider: str
    kind: str
    operation: str | None
    model: str | None
    request: dict[str, Any] = Field(
        description="The exact kwargs sent to the SDK (minus headers/transport options)."
    )
    response: dict[str, Any] | None
    tool_calls: list[ToolCall]
    tool_results: list[ToolResult] = Field(
        description="Results returned for this step's tool calls (from the following request)."
    )
    stop_reason: str | None
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    cost_usd: float | None
    latency_ms: float | None
    ttft_ms: float | None
    streamed: bool
    incomplete: bool
    error: dict[str, Any] | None
    started_at: float
    is_replay: bool
    replay_overrides: dict[str, Any] | None
    request_hash: str | None
    response_hash: str | None
    message_count: int
    parent_message_count: int | None = Field(
        description="Messages in the parent step's request: messages after "
        "`parent_message_count + 1` are new in this step."
    )

    @classmethod
    def from_step(cls, step: Step, parent_message_count: int | None) -> StepDetail:
        data = step.to_dict()
        data.pop("input_preview", None)
        data.pop("output_preview", None)
        return cls(**data, parent_message_count=parent_message_count)


DiffStatus = Literal["shared", "same", "changed", "added", "removed"]


class DiffPair(BaseModel):
    position: int = Field(description="Row number in the alignment (not a step index).")
    status: DiffStatus = Field(
        description="shared: the same recorded step (inherited by a fork); same: identical "
        "request and response; changed: request or response differs; added/removed: only "
        "in run b / run a."
    )
    request_changed: bool
    response_changed: bool
    a: StepSummaryOut | None
    b: StepSummaryOut | None


class DiffTotals(BaseModel):
    steps: int
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    latency_ms: float


class RunDiff(BaseModel):
    a: RunOut
    b: RunOut
    totals_a: DiffTotals
    totals_b: DiffTotals
    pairs: list[DiffPair]
    total_pairs: int
    offset: int
    limit: int
    first_divergence: int | None = Field(
        description="Position (row) of the first pair that isn't shared/same; None if identical."
    )


# ---------------------------------------------------------------------- replay
class ReplayStatus(BaseModel):
    sdk_installed: bool
    sdk_version: str | None
    credentials_configured: bool
    credential_source: str | None = Field(
        description="Where credentials come from (e.g. ANTHROPIC_API_KEY). Never the value."
    )
    message: str | None = Field(description="How to fix a missing SDK or key.")
    known_models: list[str]
    mock: bool = Field(
        default=False,
        description="Replays go to the offline mock API (`stepback ui --mock`): nothing is sent.",
    )


class ReplayBody(BaseModel):
    request: dict[str, Any] = Field(
        description="Complete kwargs for messages.create: model, max_tokens, messages, "
        "system, tools and any other parameters."
    )
    confirm: bool = Field(
        default=False,
        description="Must be true to send. Replays are paid API calls made only on an "
        "explicit user action.",
    )


class ReplayEstimate(BaseModel):
    model: str
    input_tokens: int
    exact: bool = Field(description="True when counted by the API's free count_tokens endpoint.")
    method: Literal["count_tokens", "scaled_from_recording", "heuristic"]
    note: str | None
    max_tokens: int
    price_known: bool
    input_cost_usd: float | None
    max_output_cost_usd: float | None = Field(description="If the response uses all max_tokens.")
    max_total_cost_usd: float | None
    original_cost_usd: float | None
    original_input_tokens: int | None
    changed: list[str] = Field(description="Top-level request keys that differ from the original.")


class ReplayOut(BaseModel):
    run_id: str = Field(description="The new forked run.")
    step_id: str
    status: Literal["ok", "error"]
    error: dict[str, Any] | None
    error_message: str | None = Field(description="The API's own error message, if it failed.")
    changed: list[str]
