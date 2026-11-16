"""DNS rebinding, cross-origin and CSRF protections of the local server."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from stepback.server import create_app
from stepback.server.security import LocalOnlyMiddleware, split_host
from stepback.storage import Run, TraceStore

BASE = "http://127.0.0.1:7777"
OWN_ORIGIN = BASE
CLIENT = {"X-Stepback-Client": "1"}


@pytest.fixture
def seeded(db_path: Path) -> Path:
    with TraceStore(db_path) as store:
        for i in range(3):
            store.create_run(Run(id=f"run_sec{i}", name=f"r{i}", created_at=float(i)))
    return db_path


@pytest.fixture
def client(seeded: Path) -> Iterator[TestClient]:
    with TestClient(create_app(seeded), base_url=BASE) as c:
        yield c


def _no_cors_headers(response: Any) -> None:
    for header in response.headers:
        assert not header.lower().startswith("access-control-"), header


# ---------------------------------------------------------------------- Host (DNS rebinding)
@pytest.mark.parametrize("host", ["127.0.0.1:7777", "localhost:7777", "[::1]:7777", "LOCALHOST"])
def test_loopback_hosts_accepted(client: TestClient, host: str) -> None:
    response = client.get("/api/health", headers={"host": host})
    assert response.status_code == 200


@pytest.mark.parametrize(
    "host",
    [
        "evil.example:7777",  # DNS-rebinding: attacker hostname resolving to 127.0.0.1
        "127.0.0.1.nip.io:7777",
        "localhost.evil.example",
        "0.0.0.0:7777",
        "192.168.1.20:7777",
    ],
)
def test_foreign_host_rejected(client: TestClient, host: str) -> None:
    for path in ("/api/health", "/api/runs", "/", "/api/openapi.json"):
        response = client.get(path, headers={"host": host})
        assert response.status_code == 400, (host, path)
        assert response.json() == {"detail": "Invalid Host header"}
        assert "r0" not in response.text


def test_missing_host_rejected() -> None:
    middleware = LocalOnlyMiddleware(app=None)  # type: ignore[arg-type]
    scope = {"type": "http", "method": "GET", "path": "/api/runs", "headers": []}
    assert middleware.check(scope) == (400, "Missing Host header")


def test_explicitly_bound_host_is_accepted(seeded: Path) -> None:
    app = create_app(seeded, allowed_hosts={"127.0.0.1", "localhost", "::1", "10.0.0.5"})
    with TestClient(app, base_url="http://10.0.0.5:7777") as c:
        assert c.get("/api/health").status_code == 200
        assert c.get("/api/health", headers={"host": "evil.example"}).status_code == 400


# ---------------------------------------------------------------------- Origin (cross-site reads)
@pytest.mark.parametrize(
    "origin",
    [
        "http://evil.example",
        "null",  # sandboxed iframes / file:// pages
        "http://localhost:5173",  # another local port is a different origin
        "http://127.0.0.1:7778",
        "https://127.0.0.1:7777",  # scheme mismatch
    ],
)
def test_cross_origin_reads_blocked(client: TestClient, origin: str) -> None:
    response = client.get("/api/runs", headers={"origin": origin})
    assert response.status_code == 403
    assert response.json() == {"detail": "Cross-origin request blocked"}
    _no_cors_headers(response)


def test_same_origin_read_allowed(client: TestClient) -> None:
    response = client.get("/api/runs", headers={"origin": OWN_ORIGIN})
    assert response.status_code == 200
    _no_cors_headers(response)


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_cross_site_fetch_metadata_blocked_for_api(client: TestClient, site: str) -> None:
    # e.g. <img src="http://127.0.0.1:7777/api/runs"> from another site or local port
    response = client.get("/api/runs", headers={"sec-fetch-site": site})
    assert response.status_code == 403


def test_cross_site_navigation_to_ui_allowed(client: TestClient) -> None:
    # Clicking a link to the UI from elsewhere must still open it.
    response = client.get(
        "/", headers={"sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate"}
    )
    assert response.status_code == 200


def test_never_any_cors_headers(client: TestClient) -> None:
    for headers in ({}, {"origin": OWN_ORIGIN}, {"origin": "http://evil.example"}):
        _no_cors_headers(client.get("/api/runs", headers=headers))


def test_preflight_rejected(client: TestClient) -> None:
    response = client.options(
        "/api/runs/run_sec0",
        headers={
            "origin": "http://evil.example",
            "access-control-request-method": "DELETE",
            "access-control-request-headers": "x-stepback-client",
        },
    )
    assert response.status_code == 403
    _no_cors_headers(response)


# ---------------------------------------------------------------------- state-changing requests
def test_delete_requires_client_header(client: TestClient, seeded: Path) -> None:
    response = client.delete("/api/runs/run_sec0")
    assert response.status_code == 403
    assert "x-stepback-client" in response.json()["detail"]
    with TraceStore(seeded) as store:
        assert store.get_run("run_sec0") is not None


def test_delete_same_origin_with_header_succeeds(client: TestClient, seeded: Path) -> None:
    response = client.delete(
        "/api/runs/run_sec0",
        headers={**CLIENT, "origin": OWN_ORIGIN, "sec-fetch-site": "same-origin"},
    )
    assert response.status_code == 204
    with TraceStore(seeded) as store:
        assert store.get_run("run_sec0") is None


def test_delete_from_non_browser_client_with_header(client: TestClient) -> None:
    # curl/scripts send no Origin or Sec-Fetch-* headers.
    assert client.delete("/api/runs/run_sec1", headers=CLIENT).status_code == 204


@pytest.mark.parametrize(
    "headers",
    [
        {**CLIENT, "origin": "http://evil.example"},
        {**CLIENT, "origin": "null"},
        {**CLIENT, "sec-fetch-site": "cross-site"},
        {**CLIENT, "sec-fetch-site": "same-site", "origin": "http://localhost:5173"},
        # A plain HTML form post: no custom header possible cross-origin.
        {"origin": "http://evil.example", "content-type": "application/x-www-form-urlencoded"},
    ],
)
def test_cross_origin_delete_blocked(
    client: TestClient, seeded: Path, headers: dict[str, str]
) -> None:
    response = client.delete("/api/runs/run_sec2", headers=headers)
    assert response.status_code == 403
    _no_cors_headers(response)
    with TraceStore(seeded) as store:
        assert store.get_run("run_sec2") is not None


def test_cross_origin_post_blocked(client: TestClient) -> None:
    response = client.post("/api/runs", headers={"origin": "http://evil.example"}, content=b"x=1")
    assert response.status_code == 403


def test_mutating_endpoints_are_never_get(client: TestClient) -> None:
    """Every operation that changes data must use POST/DELETE (never GET)."""
    schema = client.get("/api/openapi.json").json()
    mutating = {
        (path, method)
        for path, ops in schema["paths"].items()
        for method in ops
        if method not in ("get", "head")
    }
    assert mutating, "expected at least one mutating endpoint"
    assert all(method in ("post", "delete") for _, method in mutating)
    # Deleting via GET is not routed at all.
    assert client.get("/api/runs/run_sec0?delete=1").status_code == 200


# ---------------------------------------------------------------------- explicitly allowed origin
def test_explicit_dev_origin(seeded: Path) -> None:
    app = create_app(seeded, allowed_origins=["http://localhost:5173"])
    with TestClient(app, base_url=BASE) as c:
        dev = {"origin": "http://localhost:5173", "sec-fetch-site": "same-site"}
        assert c.get("/api/runs", headers=dev).status_code == 200
        assert c.delete("/api/runs/run_sec0", headers={**dev, **CLIENT}).status_code == 204
        # Still exact: other origins remain blocked, and still no CORS headers.
        other = c.get("/api/runs", headers={"origin": "http://localhost:5174"})
        assert other.status_code == 403
        _no_cors_headers(c.get("/api/runs", headers=dev))


def test_wildcards_rejected(seeded: Path) -> None:
    with pytest.raises(ValueError, match="wildcards"):
        TestClient(create_app(seeded, allowed_origins=["*"]))


# ---------------------------------------------------------------------- response headers / binding
def test_security_headers(client: TestClient) -> None:
    api = client.get("/api/runs")
    assert api.headers["x-content-type-options"] == "nosniff"
    assert api.headers["x-frame-options"] == "DENY"
    assert api.headers["cache-control"] == "no-store"
    page = client.get("/")
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert page.headers["referrer-policy"] == "no-referrer"
    blocked = client.get("/api/runs", headers={"host": "evil.example"})
    assert blocked.headers["x-frame-options"] == "DENY"


def test_ui_binds_to_loopback_by_default() -> None:
    from stepback.cli import ui_command

    host_option = next(p for p in ui_command.params if p.name == "host")
    assert host_option.default == "127.0.0.1"


def test_pick_port_skips_busy_port() -> None:
    import socket

    from stepback.cli import _pick_port

    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        taken = busy.getsockname()[1]
        import click

        with pytest.raises(click.ClickException, match="already in use"):
            _pick_port("127.0.0.1", taken)
    assert 7777 <= _pick_port("127.0.0.1", None) < 7797


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("127.0.0.1:7777", ("127.0.0.1", "7777")),
        ("[::1]:7777", ("::1", "7777")),
        ("[::1]", ("::1", None)),
        ("LocalHost", ("localhost", None)),
    ],
)
def test_split_host(value: str, expected: tuple[str, str | None]) -> None:
    assert split_host(value) == expected


@pytest.mark.parametrize("origin", ["localhost:5173", "http://localhost:5173/app", "ftp://x"])
def test_malformed_allowed_origin_rejected(seeded: Path, origin: str) -> None:
    with pytest.raises(ValueError, match="invalid origin"):
        create_app(seeded, allowed_origins=[origin])
