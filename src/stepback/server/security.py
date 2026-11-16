"""Request filtering for the local UI server.

The server holds every prompt and response you've recorded, and (with replay) can spend your
API credits, so it is locked down to the local browser tab that opened it:

* **Host check (DNS rebinding).** A malicious site can make its own hostname resolve to
  127.0.0.1 and then script requests to it. Those requests carry *its* hostname in ``Host``,
  so only loopback names (plus the explicitly bound host) are accepted.
* **Origin check (cross-site requests).** If a request has an ``Origin`` header, it must be
  the server's own origin (or one passed explicitly via ``--allow-origin``). There is no CORS
  middleware and no wildcard: cross-origin browsers never get an ``Access-Control-Allow-*``
  header, so they can neither read responses nor pass a preflight.
* **State-changing requests** (anything but GET/HEAD) additionally need:
  - the custom ``X-Stepback-Client`` header. Sending a custom header cross-origin forces a CORS
    preflight, which this server never approves, so a page on another origin can't send one.
  - ``Sec-Fetch-Site``, when the browser sends it, set to ``same-origin`` (or ``none`` for
    non-browser clients). Other localhost ports count as ``same-site``, which is rejected.
* Defensive response headers: nosniff, no framing, strict CSP (with Trusted Types), no
  referrer, and
  ``no-store`` on the API.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
SAFE_METHODS = frozenset({"GET", "HEAD"})
CLIENT_HEADER = "x-stepback-client"

# Strict CSP for the bundled UI. No inline scripts or styles, nothing from other origins,
# and Trusted Types enforcement: the browser itself rejects any attempt to inject HTML strings
# into the DOM (innerHTML & co.), so recorded content can only ever be rendered as text.
CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        "manifest-src 'self'",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
        "object-src 'none'",
        "require-trusted-types-for 'script'",
        "trusted-types 'none'",
    ]
)
_SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"content-security-policy", CONTENT_SECURITY_POLICY.encode()),
]


def split_host(value: str) -> tuple[str, str | None]:
    """``"127.0.0.1:7777"`` -> ``("127.0.0.1", "7777")``; handles ``[::1]:7777``."""
    value = value.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            return value, None
        host = value[1:end]
        rest = value[end + 1 :]
        return host, rest[1:] if rest.startswith(":") else None
    if value.count(":") == 1:
        host, port = value.split(":", 1)
        return host, port
    return value, None


def is_loopback(host: str) -> bool:
    return split_host(host)[0] in LOOPBACK_HOSTS or host.startswith("127.")


def validate_allowlists(hosts: Iterable[str], origins: Iterable[str]) -> tuple[set[str], set[str]]:
    """Normalize allowlists; reject wildcards and malformed origins."""
    host_set = {h.strip().strip("[]").lower() for h in hosts}
    origin_set = {o.strip().rstrip("/").lower() for o in origins}
    if any("*" in v for v in host_set | origin_set):
        raise ValueError("wildcards are not allowed in allowed hosts/origins")
    for origin in origin_set:
        scheme, sep, rest = origin.partition("://")
        if scheme not in ("http", "https") or not sep or not rest or "/" in rest:
            raise ValueError(f"invalid origin {origin!r}: expected scheme://host[:port]")
    return host_set, origin_set


class LocalOnlyMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        allowed_hosts: Iterable[str] = LOOPBACK_HOSTS,
        allowed_origins: Iterable[str] = (),
    ) -> None:
        self.app = app
        self.allowed_hosts, self.allowed_origins = validate_allowlists(
            allowed_hosts, allowed_origins
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        reason = self.check(scope)
        if reason is not None:
            status, message = reason
            await _reject(scope, send, status, message)
            return

        is_api = scope.get("path", "").startswith("/api")

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers += _SECURITY_HEADERS
                if is_api:
                    headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)

    def check(self, scope: Scope) -> tuple[int, str] | None:
        """Return ``(status, reason)`` if the request must be rejected."""
        headers = Headers(scope=scope)
        host_header = headers.get("host")
        if not host_header:
            return 400, "Missing Host header"
        hostname, _port = split_host(host_header)
        if hostname not in self.allowed_hosts:
            return 400, "Invalid Host header"

        scheme = "https" if scope.get("scheme") in ("https", "wss") else "http"
        own_origin = f"{scheme}://{host_header.strip().lower()}"
        origin = headers.get("origin")
        if origin is not None:
            normalized = origin.rstrip("/").lower()
            if normalized != own_origin and normalized not in self.allowed_origins:
                return 403, "Cross-origin request blocked"

        method = scope.get("method", "GET").upper()
        path = scope.get("path", "")
        fetch_site = (headers.get("sec-fetch-site") or "").lower()
        explicitly_allowed = origin is not None and origin.rstrip("/").lower() in (
            self.allowed_origins
        )

        cross_site = fetch_site in ("cross-site", "same-site")
        if path.startswith("/api") and cross_site and not explicitly_allowed:
            return 403, "Cross-site request blocked"

        if method not in SAFE_METHODS:
            if method == "OPTIONS":
                # No CORS: preflights are never approved.
                return 403, "CORS preflight requests are not accepted"
            if headers.get(CLIENT_HEADER) != "1":
                return 403, f"Missing {CLIENT_HEADER} header"
            if fetch_site not in ("", "same-origin", "none") and not explicitly_allowed:
                return 403, "Cross-site request blocked"
        return None


async def _reject(scope: Scope, send: Send, status: int, message: str) -> None:
    if scope["type"] == "websocket":
        await send({"type": "websocket.close", "code": 1008})
        return
    body = json.dumps({"detail": message}).encode()
    headers: list[tuple[bytes, Any]] = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
        *_SECURITY_HEADERS,
    ]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})
