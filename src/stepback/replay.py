"""Replay: re-send one recorded LLM call (optionally edited) and save it as a forked run.

Rules this module enforces:

* **Only the user starts a paid call.** :func:`replay_step` sends exactly one request and does
  no retries (``max_retries=0``). There is no batch or automatic replay. :func:`estimate` only
  calls the free token-counting endpoint.
* **The API key is read from the environment when a replay runs.** The client is built per
  call, so the key never reaches the database, logs, or any API response. Stored bodies
  are redacted like recorded ones.
* **Recorded history is immutable.** A replay never modifies the source run: it creates a new
  run (``parent_run_id`` = source run, ``forked_from_step_id`` = source step) whose single
  step is a sibling of the source step in the step tree.
* **One call, not the agent.** Your tool code isn't re-executed, so a fork ends at the
  replayed step. If the new response asks for a tool, that's where the fork stops.
"""

from __future__ import annotations

import inspect
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ._util import dumps, new_id, now
from .recorder.anthropic_adapter import normalize_call
from .recorder.pricing import ANTHROPIC_PRICES, estimate_cost, lookup
from .recorder.redact import redact
from .storage import Run, Step, TraceStore

ClientFactory = Callable[[], Any]

CREDENTIAL_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
MISSING_CREDENTIALS_MESSAGE = (
    "No Anthropic API key found. Replay sends the edited request with your own key, which is "
    "read from the environment of the `stepback ui` process when you replay (it's never "
    "stored). Stop the server, then run:\n\n"
    "    export ANTHROPIC_API_KEY=sk-ant-...\n"
    "    stepback ui\n\n"
    "(`ant auth login` profiles work too, with SDK versions that support them.)"
)
ALLOWED_ROLES = {"user", "assistant", "system"}
# Transport/credential options are never accepted from the editor.
FORBIDDEN_KEYS = {
    "extra_headers",
    "extra_query",
    "timeout",
    "api_key",
    "auth_token",
    "base_url",
    "default_headers",
    "http_client",
}
# Always dropped: replays are non-streaming (the result is saved in one piece).
DROPPED_KEYS = {"stream"}
_REPLAY_SUFFIX = re.compile(r" \(replay #\d+\)$")


class ReplayError(Exception):
    code = "replay_error"


class MissingCredentials(ReplayError):
    code = "missing_credentials"

    def __init__(self, message: str = MISSING_CREDENTIALS_MESSAGE) -> None:
        super().__init__(message)


class SdkMissing(ReplayError):
    code = "sdk_missing"

    def __init__(self) -> None:
        super().__init__(
            "Replay needs the Anthropic SDK: pip install 'stepback[anthropic]' (or anthropic)."
        )


class InvalidReplayRequest(ReplayError):
    code = "invalid_request"

    def __init__(self, errors: list[dict[str, str]]) -> None:
        super().__init__("; ".join(f"{e['path']}: {e['message']}" for e in errors))
        self.errors = errors


class StepNotFound(ReplayError):
    code = "not_found"


# ---------------------------------------------------------------------- credentials
@dataclass
class CredentialStatus:
    sdk_installed: bool
    sdk_version: str | None
    configured: bool
    source: str | None  # name of the env var / mechanism, never the value
    message: str | None


def credential_status() -> CredentialStatus:
    """Whether a replay could authenticate right now. Reports *where* a key is, never the key."""
    try:
        import anthropic
    except ImportError:
        return CredentialStatus(False, None, False, None, str(SdkMissing()))
    version = getattr(anthropic, "__version__", None)
    for var in CREDENTIAL_ENV_VARS:
        if os.environ.get(var, "").strip():
            return CredentialStatus(True, version, True, var, None)
    try:
        client = anthropic.Anthropic(max_retries=0)
        if getattr(client, "api_key", None) or getattr(client, "auth_token", None):
            return CredentialStatus(True, version, True, "client configuration", None)
        if getattr(client, "credentials", None):
            return CredentialStatus(True, version, True, "Anthropic profile", None)
    except Exception:
        pass
    return CredentialStatus(True, version, False, None, MISSING_CREDENTIALS_MESSAGE)


def default_client_factory() -> Any:
    """A fresh client per replay: credentials are read from the environment right now."""
    try:
        import anthropic
    except ImportError:
        raise SdkMissing() from None
    if not credential_status().configured:
        raise MissingCredentials()
    # No automatic retries: one user action sends exactly one request.
    return anthropic.Anthropic(max_retries=0, timeout=600.0)


# ---------------------------------------------------------------------- validation
def validate_request(request: Any) -> list[dict[str, str]]:
    """Structural checks with paths for inline display. Returns [] when valid."""
    errors: list[dict[str, str]] = []

    def err(path: str, message: str) -> None:
        errors.append({"path": path, "message": message})

    if not isinstance(request, dict):
        return [{"path": "request", "message": "must be a JSON object"}]

    model = request.get("model")
    if not isinstance(model, str) or not model.strip():
        err("model", "choose a model")

    max_tokens = request.get("max_tokens")
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
        err("params.max_tokens", "must be a positive integer")

    messages = request.get("messages")
    if not isinstance(messages, list) or not messages:
        err("messages", "must be a non-empty array of messages")
    else:
        for i, message in enumerate(messages):
            path = f"messages[{i}]"
            if not isinstance(message, dict):
                err(path, "must be an object with role and content")
                continue
            role = message.get("role")
            if role not in ALLOWED_ROLES:
                err(f"{path}.role", f"must be one of {', '.join(sorted(ALLOWED_ROLES))}")
            content = message.get("content")
            if isinstance(content, str):
                continue
            if not isinstance(content, list):
                err(f"{path}.content", "must be a string or an array of content blocks")
                continue
            for j, block in enumerate(content):
                if not isinstance(block, dict) or not isinstance(block.get("type"), str):
                    err(f"{path}.content[{j}]", 'must be an object with a "type"')

    system = request.get("system")
    if system is not None and not isinstance(system, (str, list)):
        err("system", "must be a string or an array of text blocks")
    elif isinstance(system, list):
        for j, block in enumerate(system):
            if not isinstance(block, dict) or not isinstance(block.get("type"), str):
                err(f"system[{j}]", 'must be an object with a "type"')

    tools = request.get("tools")
    if tools is not None:
        if not isinstance(tools, list):
            err("tools", "must be an array of tool definitions")
        else:
            for j, tool in enumerate(tools):
                if not isinstance(tool, dict):
                    err(f"tools[{j}]", "must be an object")
                elif not isinstance(tool.get("name"), str) and not isinstance(
                    tool.get("type"), str
                ):
                    err(f"tools[{j}]", 'needs a "name" (or a "type" for server tools)')

    for key in sorted(set(request) & FORBIDDEN_KEYS):
        err(f"params.{key}", "transport and credential options can't be set in a replay")

    output_format = request.get("output_format")
    if isinstance(output_format, dict) and "pydantic_model" in output_format:
        err(
            "params.output_format",
            "recorded from messages.parse() with a Python class; remove it, or use "
            "output_config.format with a JSON schema",
        )
    return errors


def prepare_request(request: dict[str, Any]) -> dict[str, Any]:
    """The kwargs actually sent: a copy without dropped keys (e.g. ``stream``)."""
    return {k: v for k, v in request.items() if k not in DROPPED_KEYS}


def changed_keys(source: dict[str, Any], edited: dict[str, Any]) -> list[str]:
    keys = (set(source) | set(edited)) - DROPPED_KEYS
    return sorted(k for k in keys if dumps(source.get(k)) != dumps(edited.get(k)))


# ---------------------------------------------------------------------- estimate
@dataclass
class Estimate:
    model: str
    input_tokens: int
    exact: bool
    method: str  # "count_tokens" | "scaled_from_recording" | "heuristic"
    note: str | None
    max_tokens: int
    price_known: bool
    input_cost_usd: float | None
    max_output_cost_usd: float | None
    max_total_cost_usd: float | None
    original_cost_usd: float | None
    original_input_tokens: int | None
    changed: list[str] = field(default_factory=list)


def estimate(
    store: TraceStore,
    step_id: str,
    request: dict[str, Any],
    *,
    client_factory: ClientFactory | None = None,
) -> Estimate:
    """Model, input tokens and cost range for a replay. Never sends a paid request.

    Uses the free ``count_tokens`` endpoint when credentials are available, and falls back
    to a local estimate otherwise.
    """
    source = _source_step(store, step_id)
    errors = validate_request(request)
    if errors:
        raise InvalidReplayRequest(errors)
    req = prepare_request(request)
    model = str(req["model"])
    max_tokens = int(req["max_tokens"])

    tokens: int | None = None
    method, note = "heuristic", None
    try:
        client = (client_factory or default_client_factory)()
        tokens = _count_tokens(client, req)
        method = "count_tokens"
    except MissingCredentials:
        note = "No API key: estimated locally."
    except SdkMissing:
        note = "Anthropic SDK not installed: estimated locally."
    except Exception as exc:
        note = f"Token counting failed ({type(exc).__name__}): estimated locally."
    if tokens is None:
        tokens, method = _local_estimate(source, req)

    input_cost = estimate_cost(model, {"input_tokens": tokens})
    output_cost = estimate_cost(model, {"output_tokens": max_tokens})
    price_known = lookup(model) is not None
    recorded_in = (
        sum(
            t or 0
            for t in (source.input_tokens, source.cache_read_tokens, source.cache_write_tokens)
        )
        or None
    )
    return Estimate(
        model=model,
        input_tokens=tokens,
        exact=method == "count_tokens",
        method=method,
        note=note,
        max_tokens=max_tokens,
        price_known=price_known,
        input_cost_usd=input_cost,
        max_output_cost_usd=output_cost,
        max_total_cost_usd=(
            round(input_cost + output_cost, 8)
            if input_cost is not None and output_cost is not None
            else None
        ),
        original_cost_usd=source.cost_usd,
        original_input_tokens=recorded_in,
        changed=changed_keys(source.request, req),
    )


TOKEN_COUNTING_BETA = "token-counting-2024-11-01"


def _count_tokens(client: Any, req: dict[str, Any]) -> int:
    request = dict(req)
    resource = client.beta.messages if request.get("betas") else client.messages
    method = getattr(resource, "count_tokens", None)
    if method is None:
        # SDKs from before token counting left beta (e.g. anthropic 0.40) only have it here.
        method = client.beta.messages.count_tokens
        request["betas"] = [*(request.get("betas") or []), TOKEN_COUNTING_BETA]
    accepted = _accepted_params(method)
    kwargs = {k: v for k, v in request.items() if accepted is None or k in accepted}
    kwargs.pop("max_tokens", None)
    result = method(**kwargs)
    return int(result.input_tokens)


def _local_estimate(source: Step, req: dict[str, Any]) -> tuple[int, str]:
    def size(r: dict[str, Any]) -> int:
        return len(dumps([r.get("system"), r.get("messages"), r.get("tools")]))

    recorded = sum(
        t or 0 for t in (source.input_tokens, source.cache_read_tokens, source.cache_write_tokens)
    )
    if recorded and size(source.request):
        return max(1, round(recorded * size(req) / size(source.request))), "scaled_from_recording"
    return max(1, round(size(req) / 3.5)), "heuristic"


def _accepted_params(method: Any) -> set[str] | None:
    try:
        params = inspect.signature(method).parameters
    except (TypeError, ValueError):
        return None
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return None
    return {name for name in params if name not in ("self",)}


# ---------------------------------------------------------------------- replay
@dataclass
class ReplayResult:
    run_id: str
    step_id: str
    status: str  # "ok" | "error"
    error: dict[str, Any] | None
    error_message: str | None
    changed: list[str]


def replay_step(
    store: TraceStore,
    step_id: str,
    request: dict[str, Any],
    *,
    client_factory: ClientFactory | None = None,
) -> ReplayResult:
    """Send one (edited) request for ``step_id`` and save the result as a new forked run.

    API errors (auth, rate limits, invalid requests...) are saved as a failed step in the
    fork. Missing credentials, an invalid edit or a missing SDK raise before anything is
    sent or saved.
    """
    source = _source_step(store, step_id)
    errors = validate_request(request)
    if errors:
        raise InvalidReplayRequest(errors)
    req = prepare_request(request)
    client = (client_factory or default_client_factory)()
    source_run = store.get_run(source.run_id)
    base_name = _REPLAY_SUFFIX.sub("", source_run.name) if source_run else source.run_id
    changed = changed_keys(source.request, req)

    fork_id = new_id("run")
    store.create_run(
        Run(
            id=fork_id,
            name=f"{base_name} (replay #{source.idx})",
            created_at=now(),
            parent_run_id=source.run_id,
            forked_from_step_id=source.id,
            metadata={"replay_of_step": source.id, "changed": changed},
        )
    )

    use_beta = bool(req.get("betas"))
    resource = client.beta.messages if use_beta else client.messages
    accepted = _accepted_params(resource.create)
    kwargs = dict(req)
    if accepted is not None:
        extra = {k: kwargs.pop(k) for k in list(kwargs) if k not in accepted}
        if extra:
            kwargs["extra_body"] = extra

    started_at = time.time()
    t0 = time.perf_counter()
    response: Any = None
    error: BaseException | None = None
    try:
        response = resource.create(**kwargs)
    except TypeError as exc:
        if "authentication" in str(exc).lower():
            store.delete_run(fork_id)  # nothing was sent
            raise MissingCredentials() from None
        error = exc
    except Exception as exc:  # APIStatusError, APIConnectionError, ...
        error = exc
    latency_ms = (time.perf_counter() - t0) * 1000

    normalized = normalize_call(req, response=response, error=error)
    usage = normalized.usage or {}
    step = Step(
        id=new_id("step"),
        run_id=fork_id,
        idx=source.idx,
        parent_step_id=source.parent_step_id,
        provider="anthropic",
        operation="beta.messages.create" if use_beta else "messages.create",
        model=normalized.model,
        request=redact(normalized.request),
        response=redact(normalized.response),
        tool_calls=redact(normalized.tool_calls),
        stop_reason=normalized.stop_reason,
        input_tokens=_int(usage.get("input_tokens")),
        output_tokens=_int(usage.get("output_tokens")),
        cache_read_tokens=_int(usage.get("cache_read_input_tokens")),
        cache_write_tokens=_int(usage.get("cache_creation_input_tokens")),
        cost_usd=estimate_cost(normalized.model, usage),
        latency_ms=latency_ms,
        error=redact(normalized.error),
        started_at=started_at,
        is_replay=True,
        replay_overrides={"source_step_id": source.id, "changed": changed},
    )
    store.insert_step(step)
    store.end_run(fork_id, "error" if error else "ok", now())
    return ReplayResult(
        run_id=fork_id,
        step_id=step.id,
        status="error" if error else "ok",
        error=step.error,
        error_message=api_error_message(step.error) if step.error else None,
        changed=changed,
    )


def api_error_message(error: dict[str, Any] | None) -> str | None:
    """The API's own words: ``"rate_limit_error: Number of request tokens..."``."""
    if not error:
        return None
    body = error.get("body")
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        inner = body["error"]
        if inner.get("message"):
            return f"{inner.get('type', 'error')}: {inner['message']}"
    return f"{error.get('type', 'Error')}: {error.get('message', '')}".strip()


def known_models() -> list[str]:
    """Model ids from the price table, newest families first (for the editor's picker)."""
    return list(ANTHROPIC_PRICES)


def _source_step(store: TraceStore, step_id: str) -> Step:
    step = store.get_step(step_id)
    if step is None:
        raise StepNotFound(f"step {step_id!r} not found")
    return step


def _int(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) else None


__all__ = [
    "CredentialStatus",
    "Estimate",
    "InvalidReplayRequest",
    "MissingCredentials",
    "ReplayError",
    "ReplayResult",
    "SdkMissing",
    "StepNotFound",
    "api_error_message",
    "credential_status",
    "estimate",
    "known_models",
    "replay_step",
    "validate_request",
]
