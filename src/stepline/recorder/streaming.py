"""Observe SDK stream objects without changing their type or delaying chunks.

Both the Anthropic and OpenAI Python SDKs build ``Stream`` / ``AsyncStream`` objects whose
``__iter__`` pulls from a private ``_iterator`` generator. We replace that generator on the
*same* object with a pass-through that notes each event and then yields it unchanged. The
user keeps the exact object the SDK returned (``isinstance`` checks still pass), and each chunk
is handed over right after a list append.

If an SDK version doesn't have this layout, we degrade gracefully: the stream is returned
untouched and the call is recorded without a response body.
"""

from __future__ import annotations

import weakref
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

from .._util import warn_once
from .base import CallHandle

IsToken = Callable[[Any], bool]


def instrument_stream(stream: Any, handle: CallHandle, is_token: IsToken) -> Any:
    """Instrument a sync stream in place and return it."""
    try:
        inner: Iterator[Any] = stream._iterator  # materializes the SDK's generator
    except Exception as exc:
        warn_once("stream-layout", "unrecognized stream object; recording without body", exc)
        handle.finish(incomplete=True)
        return stream

    def observe() -> Iterator[Any]:
        complete = False
        try:
            for event in inner:
                handle.on_event(event, is_token(event))
                yield event
            complete = True
        except GeneratorExit:
            raise
        except BaseException as exc:
            handle.finish(error=exc, incomplete=True)
            raise
        finally:
            handle.finish(incomplete=not complete)

    try:
        stream.__dict__["_iterator"] = observe()
        _patch_close(stream, handle, is_async=False)
    except Exception as exc:
        warn_once("stream-patch", "could not instrument stream; recording without body", exc)
        handle.finish(incomplete=True)
    return stream


def instrument_async_stream(stream: Any, handle: CallHandle, is_token: IsToken) -> Any:
    """Instrument an async stream in place and return it."""
    try:
        inner: AsyncIterator[Any] = stream._iterator
    except Exception as exc:
        warn_once("stream-layout", "unrecognized stream object; recording without body", exc)
        handle.finish(incomplete=True)
        return stream

    async def observe() -> AsyncIterator[Any]:
        complete = False
        try:
            async for event in inner:
                handle.on_event(event, is_token(event))
                yield event
            complete = True
        except GeneratorExit:
            raise
        except BaseException as exc:
            handle.finish(error=exc, incomplete=True)
            raise
        finally:
            handle.finish(incomplete=not complete)

    try:
        stream.__dict__["_iterator"] = observe()
        _patch_close(stream, handle, is_async=True)
    except Exception as exc:
        warn_once("stream-patch", "could not instrument stream; recording without body", exc)
        handle.finish(incomplete=True)
    return stream


def _patch_close(stream: Any, handle: CallHandle, *, is_async: bool) -> None:
    """Record a partially-read stream as soon as the user closes it.

    Only a weak reference to the stream is kept, so no reference cycle is created and a
    dropped stream is still released promptly.
    """
    ref = weakref.ref(stream)
    cls_close = type(stream).close

    if is_async:

        async def aclose() -> None:
            target = ref()
            try:
                if target is not None:
                    await cls_close(target)
            finally:
                handle.finish(incomplete=True)

        stream.__dict__["close"] = aclose
    else:

        def close() -> None:
            target = ref()
            try:
                if target is not None:
                    cls_close(target)
            finally:
                handle.finish(incomplete=True)

        stream.__dict__["close"] = close
