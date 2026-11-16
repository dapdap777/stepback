"""Adapter for the official Anthropic Python SDK (``anthropic``).

Covers ``Anthropic``/``AsyncAnthropic`` and the platform clients that share their surface
(Bedrock, Vertex, Foundry). Patched methods, on both ``client.messages`` and
``client.beta.messages``:

* ``create`` (including ``stream=True``)
* ``parse`` (structured outputs)
* ``stream`` (the ``with client.messages.stream(...) as s:`` helper)

The SDK is imported lazily and only for type checks, so Stepback itself doesn't need it.
"""

from __future__ import annotations

import functools
import inspect
import json
from typing import TYPE_CHECKING, Any, TypeVar

from .._util import snapshot_containers, to_jsonable, warn_once
from .base import CallHandle, NormalizedCall, normalize_error
from .streaming import instrument_async_stream, instrument_stream

if TYPE_CHECKING:
    from .recorder import Recorder

ClientT = TypeVar("ClientT")

#: Tested range of the ``anthropic`` package: [floor, cap). Mirrors pyproject.toml.
SUPPORTED_ANTHROPIC = ((0, 40), (3, 0))

_WRAPPED_FLAG = "_stepback_wrapped"
# Request kwargs never stored: headers may carry credentials; the rest is transport config.
_DROP_KWARGS = {"extra_headers", "extra_query", "timeout"}
_TOOL_CALL_TYPES = {"tool_use", "server_tool_use", "mcp_tool_use"}


def _is_token_event(event: Any) -> bool:
    return getattr(event, "type", None) == "content_block_delta"


def _is_omitted(value: Any) -> bool:
    # anthropic.Omit / NotGiven sentinels, detected without importing the SDK.
    return type(value).__name__ in ("Omit", "NotGiven")


def snapshot_request(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Cheap copy of request kwargs taken on the caller's thread."""
    return {
        key: snapshot_containers(value)
        for key, value in kwargs.items()
        if key not in _DROP_KWARGS and not _is_omitted(value)
    }


class AnthropicAdapter:
    name = "anthropic"

    # ------------------------------------------------------------------ detection
    def matches(self, client: object) -> bool:
        module = type(client).__module__ or ""
        return module.split(".", 1)[0] == "anthropic" and hasattr(client, "messages")

    # ------------------------------------------------------------------ install
    def install(self, client: ClientT, recorder: Recorder) -> ClientT:
        if getattr(client, _WRAPPED_FLAG, False):
            return client
        check_sdk_version()
        resources = []
        for path in (("messages",), ("beta", "messages")):
            try:
                obj: Any = client
                for attr in path:
                    obj = getattr(obj, attr)
                resources.append((".".join(path), obj))
            except Exception:
                continue
        for prefix, resource in resources:
            try:
                self._patch_resource(resource, prefix, recorder)
            except Exception as exc:
                warn_once(f"install-{prefix}", f"could not instrument client.{prefix}", exc)
        self._patch_copy(client, recorder)
        try:
            setattr(client, _WRAPPED_FLAG, True)
        except Exception:
            pass
        return client

    def _patch_copy(self, client: Any, recorder: Recorder) -> None:
        """``with_options(...)`` / ``copy(...)`` return new clients: wrap those too."""
        for name in ("with_options", "copy"):
            original = getattr(client, name, None)
            if not callable(original):
                continue

            def make(orig: Any) -> Any:
                @functools.wraps(orig)
                def wrapper(*args: Any, **kwargs: Any) -> Any:
                    new_client = orig(*args, **kwargs)
                    try:
                        return self.install(new_client, recorder)
                    except Exception:  # pragma: no cover - defensive
                        return new_client

                return wrapper

            try:
                setattr(client, name, make(original))
            except Exception:
                pass

    def _patch_resource(self, resource: Any, prefix: str, recorder: Recorder) -> None:
        if getattr(resource, _WRAPPED_FLAG, False):
            return
        # The SDK's argument-validation decorator hides coroutine functions behind a sync
        # wrapper, so detect async resources by class name (AsyncMessages, ...).
        resource_is_async = type(resource).__name__.startswith("Async")
        for method in ("create", "parse"):
            original = getattr(resource, method, None)
            if original is None:
                continue
            if resource_is_async or inspect.iscoroutinefunction(original):
                wrapped = self._wrap_async_call(original, f"{prefix}.{method}", recorder)
            else:
                wrapped = self._wrap_sync_call(original, f"{prefix}.{method}", recorder)
            setattr(resource, method, wrapped)
        original_stream = getattr(resource, "stream", None)
        if original_stream is not None:
            resource.stream = self._wrap_stream_helper(
                original_stream, f"{prefix}.stream", recorder
            )
        setattr(resource, _WRAPPED_FLAG, True)

    def _wrap_sync_call(self, original: Any, operation: str, recorder: Recorder) -> Any:
        adapter = self

        @functools.wraps(original)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            handle = _begin(recorder, adapter, operation, kwargs)
            try:
                result = original(*args, **kwargs)
            except BaseException as exc:
                if handle is not None:
                    handle.finish(error=exc)
                raise
            if handle is None:
                return result
            if inspect.isawaitable(result):
                # An async method we failed to detect up front: finish once awaited.
                return _await_and_finish(result, handle)
            if handle.streamed:
                return _safe_instrument(result, handle, is_async=False)
            handle.finish(response=result)
            return result

        return wrapper

    def _wrap_async_call(self, original: Any, operation: str, recorder: Recorder) -> Any:
        adapter = self

        @functools.wraps(original)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            handle = _begin(recorder, adapter, operation, kwargs)
            try:
                result = await original(*args, **kwargs)
            except BaseException as exc:
                if handle is not None:
                    handle.finish(error=exc)
                raise
            if handle is None:
                return result
            if handle.streamed:
                return _safe_instrument(result, handle, is_async=True)
            handle.finish(response=result)
            return result

        return wrapper

    def _wrap_stream_helper(self, original: Any, operation: str, recorder: Recorder) -> Any:
        adapter = self

        @functools.wraps(original)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            manager = original(*args, **kwargs)
            try:
                request = snapshot_request(kwargs)
                request["stream"] = True
            except Exception as exc:
                warn_once("snapshot", "failed to snapshot request", exc)
                return manager
            return _RecordingStreamManager(manager, recorder, adapter, operation, request)

        return wrapper

    # ------------------------------------------------------------------ normalize
    def normalize(self, call: CallHandle) -> NormalizedCall:
        return normalize_call(
            call.request,
            response=call.response,
            events=call.events if call.streamed else None,
            error=call.error,
        )


def normalize_call(
    request: dict[str, Any],
    *,
    response: Any = None,
    events: list[Any] | None = None,
    error: BaseException | None = None,
) -> NormalizedCall:
    """Normalize one Anthropic call. Shared by the recorder and by replay.

    ``events`` (raw stream events) take precedence over ``response`` when given.
    """
    jrequest = _jsonable_request(request)
    if events is not None:
        jresponse = accumulate_stream(events) if events else None
    else:
        jresponse = _jsonable_response(response)

    tool_calls: list[dict[str, Any]] = []
    usage: dict[str, Any] | None = None
    stop_reason: str | None = None
    model = jrequest.get("model")
    if isinstance(jresponse, dict):
        for block in jresponse.get("content") or []:
            if isinstance(block, dict) and block.get("type") in _TOOL_CALL_TYPES:
                tool_calls.append(
                    {
                        "id": block.get("id"),
                        "name": block.get("name"),
                        "input": block.get("input"),
                        "type": block.get("type"),
                    }
                )
        usage = jresponse.get("usage") if isinstance(jresponse.get("usage"), dict) else None
        stop_reason = jresponse.get("stop_reason")
        model = jresponse.get("model") or model

    return NormalizedCall(
        model=str(model) if model else None,
        request=jrequest,
        response=jresponse,
        tool_calls=tool_calls,
        tool_results_in_request=extract_tool_results(jrequest.get("messages")),
        usage=usage,
        stop_reason=stop_reason,
        error=normalize_error(error),
    )


def _parse_version(version: str) -> tuple[int, int]:
    parts = []
    for piece in version.split(".")[:2]:
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 2:
        parts.append(0)
    return parts[0], parts[1]


def check_sdk_version() -> bool:
    """Warn (once) if the installed anthropic SDK is outside the tested range."""
    try:
        from importlib.metadata import version

        installed = version("anthropic")
    except Exception:
        return True
    floor, cap = SUPPORTED_ANTHROPIC
    if floor <= _parse_version(installed) < cap:
        return True
    warn_once(
        "sdk-version",
        f"anthropic {installed} is outside the tested range "
        f"(>={floor[0]}.{floor[1]},<{cap[0]}.{cap[1]}); some calls may be recorded "
        "without a response body",
    )
    return False


# ---------------------------------------------------------------------- hot-path helpers
def _begin(
    recorder: Recorder, adapter: AnthropicAdapter, operation: str, kwargs: dict[str, Any]
) -> CallHandle | None:
    try:
        request = snapshot_request(kwargs)
        return recorder.begin(adapter, operation, request, streamed=bool(kwargs.get("stream")))
    except Exception as exc:
        warn_once("begin", "failed to start recording a call", exc)
        return None


async def _await_and_finish(awaitable: Any, handle: CallHandle) -> Any:
    try:
        result = await awaitable
    except BaseException as exc:
        handle.finish(error=exc)
        raise
    if handle.streamed:
        return _safe_instrument(result, handle, is_async=True)
    handle.finish(response=result)
    return result


def _safe_instrument(result: Any, handle: CallHandle, *, is_async: bool) -> Any:
    try:
        if is_async:
            return instrument_async_stream(result, handle, _is_token_event)
        return instrument_stream(result, handle, _is_token_event)
    except Exception as exc:  # pragma: no cover - instrument_* already guard themselves
        warn_once("instrument", "failed to instrument a stream", exc)
        handle.finish(incomplete=True)
        return result


class _RecordingStreamManager:
    """Wraps ``MessageStreamManager`` / ``AsyncMessageStreamManager``.

    The HTTP request is only sent on ``__enter__``, so that's where timing starts.
    """

    def __init__(
        self,
        manager: Any,
        recorder: Recorder,
        adapter: AnthropicAdapter,
        operation: str,
        request: dict[str, Any],
    ) -> None:
        self._manager = manager
        self._recorder = recorder
        self._adapter = adapter
        self._operation = operation
        self._request = request
        self._handle: CallHandle | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._manager, name)

    def _start(self) -> CallHandle | None:
        self._handle = self._recorder.begin(
            self._adapter, self._operation, self._request, streamed=True
        )
        return self._handle

    def _instrument(self, message_stream: Any, *, is_async: bool) -> None:
        handle = self._handle
        if handle is None:
            return
        raw = getattr(message_stream, "_raw_stream", None)
        if raw is None:
            warn_once("stream-layout", "unrecognized MessageStream; recording without body")
            handle.finish(incomplete=True)
            return
        _safe_instrument(raw, handle, is_async=is_async)

    def __enter__(self) -> Any:
        handle = self._start()
        try:
            message_stream = self._manager.__enter__()
        except BaseException as exc:
            if handle is not None:
                handle.finish(error=exc)
            raise
        self._instrument(message_stream, is_async=False)
        return message_stream

    def __exit__(self, *exc_info: Any) -> Any:
        try:
            return self._manager.__exit__(*exc_info)
        finally:
            if self._handle is not None:
                self._handle.finish(incomplete=True)  # no-op if the stream was fully read

    async def __aenter__(self) -> Any:
        handle = self._start()
        try:
            message_stream = await self._manager.__aenter__()
        except BaseException as exc:
            if handle is not None:
                handle.finish(error=exc)
            raise
        self._instrument(message_stream, is_async=True)
        return message_stream

    async def __aexit__(self, *exc_info: Any) -> Any:
        try:
            return await self._manager.__aexit__(*exc_info)
        finally:
            if self._handle is not None:
                self._handle.finish(incomplete=True)


# ---------------------------------------------------------------------- normalization helpers
def _jsonable_request(request: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in request.items():
        if key == "output_format" and isinstance(value, type):
            schema: Any = None
            schema_fn = getattr(value, "model_json_schema", None)
            if callable(schema_fn):
                try:
                    schema = schema_fn()
                except Exception:
                    schema = None
            out[key] = {"pydantic_model": value.__name__, "schema": schema}
        else:
            out[key] = to_jsonable(value)
    return out


def _jsonable_response(response: Any) -> dict[str, Any] | None:
    if response is None:
        return None
    # Raw-response wrappers (client.messages.with_raw_response.create) expose .parse().
    if not hasattr(response, "content") and callable(getattr(response, "parse", None)):
        try:
            response = response.parse()
        except Exception:
            return None
    data = to_jsonable(response)
    return data if isinstance(data, dict) else {"value": data}


def extract_tool_results(messages: Any) -> list[dict[str, Any]]:
    """Tool results carried by the last user message of a request."""
    if not isinstance(messages, list):
        return []
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        results = []
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    results.append(
                        {
                            "tool_use_id": block.get("tool_use_id"),
                            "content": block.get("content"),
                            "is_error": bool(block.get("is_error", False)),
                        }
                    )
        return results
    return []


def accumulate_stream(events: list[Any]) -> dict[str, Any] | None:
    """Rebuild a Message dict from raw ``RawMessageStreamEvent`` objects."""
    message: dict[str, Any] | None = None
    blocks: dict[int, dict[str, Any]] = {}
    json_buffers: dict[int, str] = {}

    for raw_event in events:
        event = to_jsonable(raw_event)
        if not isinstance(event, dict):
            continue
        etype = event.get("type")
        if etype == "message_start":
            message = dict(event.get("message") or {})
            message["content"] = []
        elif etype == "content_block_start":
            index = int(event.get("index", len(blocks)))
            blocks[index] = dict(event.get("content_block") or {})
        elif etype == "content_block_delta":
            index = int(event.get("index", 0))
            block = blocks.setdefault(index, {"type": "text", "text": ""})
            delta = event.get("delta") or {}
            dtype = delta.get("type")
            if dtype == "text_delta":
                block["text"] = block.get("text", "") + delta.get("text", "")
            elif dtype == "input_json_delta":
                json_buffers[index] = json_buffers.get(index, "") + delta.get("partial_json", "")
            elif dtype == "thinking_delta":
                block["thinking"] = block.get("thinking", "") + delta.get("thinking", "")
            elif dtype == "signature_delta":
                block["signature"] = delta.get("signature")
            elif dtype == "citations_delta":
                block.setdefault("citations", []).append(delta.get("citation"))
        elif etype == "content_block_stop":
            index = int(event.get("index", 0))
            _finalize_json(blocks, json_buffers, index)
        elif etype == "message_delta":
            if message is None:
                message = {"content": []}
            delta = event.get("delta") or {}
            for key in ("stop_reason", "stop_sequence", "stop_details"):
                if delta.get(key) is not None:
                    message[key] = delta[key]
            usage = event.get("usage") or {}
            merged = dict(message.get("usage") or {})
            merged.update({k: v for k, v in usage.items() if v is not None})
            message["usage"] = merged

    for index in list(json_buffers):
        _finalize_json(blocks, json_buffers, index)
    if message is None and not blocks:
        return None
    if message is None:
        message = {}
    message["content"] = [blocks[i] for i in sorted(blocks)]
    return message


def _finalize_json(blocks: dict[int, dict[str, Any]], buffers: dict[int, str], index: int) -> None:
    if index not in buffers:
        return
    text = buffers.pop(index)
    block = blocks.setdefault(index, {})
    try:
        block["input"] = json.loads(text) if text else {}
    except ValueError:
        block["input"] = {"_partial_json": text}
