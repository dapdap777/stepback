# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0] - Unreleased

First release. Published on PyPI as `stepback-debugger`; the import and the CLI are `stepback`.

### Added

- **Recorder:** `stepback.wrap(client)` for the Anthropic Python SDK (`anthropic>=0.40,<3`),
  sync and async. Records `messages.create` (including `stream=True`), `messages.stream` and
  `messages.parse` on `client.messages` and `client.beta.messages`: request, response, tool
  calls with their linked results, tokens (including cache), estimated cost, latency, time to
  first token, and errors. It uses a background writer and flushes at exit, never raises into
  your code, and redacts key-like strings without ever storing headers.
- `stepback.run(name)` context manager/decorator for named runs; `stepback.configure()`;
  `STEPBACK_DISABLED`, `STEPBACK_DB` and `STEPBACK_DIR`.
- **Storage:** SQLite (`./.stepback/traces.db`, WAL) with versioned migrations; runs, and steps
  linked into a tree by `parent_step_id`.
- **Cost estimation** for Claude models, with overrides in `.stepback/prices.toml`.
- **CLI:** `stepback ls`, `show`, `rm`, `prices`, `demo`, `ui`, `openapi`.
- **Local web UI:** run list, a virtualized timeline with per-step tokens, cost, latency and
  a waterfall, a step panel (messages, tool calls, request, raw JSON), run diffs, dark and
  light themes, keyboard navigation.
- **Replay:** edit a step (model, system prompt, messages, tools, parameters), review a token
  and cost estimate, and send one call. The result is saved as a forked run; API errors are
  kept as failed steps. `stepback ui --mock` replays against an offline mock.
- **Security model** for the UI server: loopback bind, Host and Origin checks, no CORS, a
  custom header required for writes, strict CSP with Trusted Types. See `SECURITY.md`.
- **Example agent** (`examples/weather_agent.py`) with an offline mock mode; `stepback demo`.

[0.1.0]: https://github.com/dapdap777/stepback/releases/tag/v0.1.0
