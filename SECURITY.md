# Security

Stepline is a local development tool. It records your prompts, model outputs and tool data to
a file on your machine and serves them to your browser. This document describes what that means
for security and how to report problems.

## Reporting a vulnerability

Please **don't open a public issue** for security problems. Use GitHub's private vulnerability
reporting instead: go to the repository's **Security** tab and choose **Report a vulnerability**.
If that option isn't available, open an issue that asks for a private contact, without giving
details of the problem.

Please include the Stepline version, how to reproduce the issue, and what an attacker could
achieve. We'll acknowledge reports as soon as we can and coordinate a fix and disclosure with you.

## What Stepline stores

- **Traces are not encrypted.** `./.stepline/traces.db` contains every recorded request and
  response: system prompts, messages, tool inputs and tool results. Treat the directory like
  any other file holding sensitive data. Stepline writes a `.gitignore` into `.stepline/` so
  traces aren't committed by default.
- **API keys are never stored on purpose.** Only request bodies are recorded, never headers or
  client configuration. As a second line of defense, strings that look like credentials (for
  example `sk-ant-…`, `AKIA…`, `ghp_…`, bearer tokens, or values under keys such as
  `api_key`/`password`) are replaced with `[REDACTED]` before writing. This redaction is
  best-effort pattern matching, not a guarantee. A secret in an unrecognized format inside a
  prompt will be stored as-is.
- **There is no telemetry.** Stepline makes no network requests of its own. The only outbound
  request is a **replay**, which you trigger explicitly and which goes to the Anthropic API.

## The local web UI (`stepline ui`)

The UI server can read everything you've recorded and, through replay, spend API credits. It
is locked down to the browser tab that opened it:

| Threat | Mitigation |
|---|---|
| Other machines on the network | Binds to `127.0.0.1` by default. `--host` can bind elsewhere, with a warning. |
| DNS rebinding (a malicious site making its own hostname resolve to 127.0.0.1) | The `Host` header must be `127.0.0.1`, `localhost`, `::1` or the explicitly bound host; anything else gets HTTP 400. |
| Cross-site reads | A request that carries an `Origin` header must come from the UI's own origin. There is no CORS: `Access-Control-Allow-*` headers are never sent, and preflight requests are rejected. Requests to `/api/*` marked `Sec-Fetch-Site: cross-site` or `same-site` are rejected. |
| Cross-site writes (CSRF) | Every state-changing endpoint (delete, replay) is `POST` or `DELETE`, requires an `X-Stepline-Client` header (which a cross-origin page can't send without a preflight), and is refused unless `Sec-Fetch-Site` is same-origin. Replay also requires `confirm: true` in the request body. |
| Malicious content in recorded data (XSS) | All recorded content is rendered as text, or as markdown without raw HTML. Images in model output are never loaded. The server sends a strict Content-Security-Policy (`default-src 'none'`, `script-src 'self'`, no inline scripts or styles) with **Trusted Types** enforced, so the browser itself refuses to inject HTML strings. Responses also set `X-Frame-Options: DENY`, `nosniff`, `no-referrer`, and `Cache-Control: no-store` on the API. |
| Supply chain at runtime | The UI is bundled (code, fonts and icons) and makes no requests to other origins. The Swagger/ReDoc pages, which load from a CDN, are disabled. |

Every row is covered by tests in `tests/test_server_security.py` and the Playwright suite in
`frontend/e2e/` (including an XSS test that seeds `<script>`, `onerror`, `javascript:` and
remote-image payloads into every rendered field).

**Out of scope.** Other software running as your user on the same machine can read
`.stepline/traces.db` directly, and can call the local API with the required header (for
example with `curl`). Stepline doesn't defend against local processes you already run.

## Replay and API keys

- The key is read from the environment of the `stepline ui` process (`ANTHROPIC_API_KEY`,
  `ANTHROPIC_AUTH_TOKEN`, or an `ant auth login` profile) **at the moment you replay**. It is
  never stored, logged, or returned by any endpoint; the status endpoint reports only the name
  of the variable. Tests check this with a sentinel key.
- A replay sends exactly one request (no automatic retries) and only after an explicit
  confirmation in the UI.
- `stepline ui --mock` sends replays to a bundled offline mock instead, so nothing leaves the
  machine.

## Supported versions

Stepline is pre-1.0. Security fixes go into the latest release.
