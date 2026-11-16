# Contributing to Stepline

Thanks for helping! Bug reports, small fixes and new SDK adapters are all welcome. For anything
larger than a bug fix, please open an issue first so we can agree on the approach.

## Development setup

You'll need Python 3.10+, [uv](https://docs.astral.sh/uv/), and Node 20+ (for the UI).

```bash
git clone https://github.com/dapdap777/stepline && cd stepline
uv sync                                    # Python env with dev tools
(cd frontend && npm ci && npm run build)   # UI, built into src/stepline/server/static
uv run stepline demo && uv run stepline ui
```

UI development with hot reload: run `uv run stepline ui --no-open` in one terminal and
`cd frontend && npm run dev` in another, then open http://localhost:5173.

## Checks

| Command | What it runs |
|---|---|
| `make check` | ruff, ruff format, mypy (strict), pytest, and pytest again on the oldest supported `anthropic` SDK (exactly what CI runs for Python) |
| `make fmt` | auto-fix lint and formatting |
| `cd frontend && npm run build` | typecheck and production build |
| `cd frontend && npm run gen:api` | regenerate the UI's API types after changing the backend schema; CI fails if they're stale |
| `cd frontend && npm run e2e` | Playwright: seeds databases, starts real `stepline ui` servers, and runs the smoke, timeline, XSS and replay tests |

Tests never call the real Anthropic API. Python tests use the SDK with an in-memory transport
(`tests/fakes.py`); the e2e replay tests point the SDK at `frontend/e2e/fake_anthropic.py`.

## Ground rules

- **Never break the user's app.** Recorder code runs inside other people's programs. Catch your
  own errors (see `warn_once`), keep the hot path cheap, and re-raise SDK exceptions unchanged.
- **Recorded content is untrusted.** In the UI, render it as React text or through
  `SafeMarkdown`; never use `dangerouslySetInnerHTML`. The CSP enforces this with Trusted Types.
- **Nothing leaves the machine** except a replay the user explicitly confirms. No telemetry, no
  CDN assets.
- **Only claim what's tested.** If you add a feature, add tests. If the README mentions it,
  there should be a test that covers it.
- Keep commits focused, with a message that says why the change was made.

## Adding an SDK adapter

Adapters implement `stepline.recorder.base.ProviderAdapter`:

- `matches(client)`: does this adapter handle the client?
- `install(client, recorder)`: patch the client in place. Call `recorder.begin(...)` before each
  request and `handle.finish(...)` after it, and use `instrument_stream` for streams.
- `normalize(call)`: turn the raw objects into a `NormalizedCall` (runs on the writer thread).

`AnthropicAdapter` in `src/stepline/recorder/anthropic_adapter.py` is the reference. Register a
new adapter in `stepline/recorder/__init__.py` and add tests with a mocked transport, in the
style of `tests/test_recorder_sync.py`.

## Good first issues

These are self-contained and a good way to learn the codebase. Comment on (or open) an issue
before starting so work isn't duplicated.

1. **`stepline export RUN [--out FILE]`:** write a run (and its inherited steps) as JSON,
   reusing `TraceStore.get_lineage`. Add a CLI test.
2. **Cache hit rate in the step panel:** show `cache_read / (input + cache_read + cache_write)`
   next to the token counts in `StepDetail.tsx`, and as a column in `stepline show`.
3. **`stepline ls --since 2h` / `--name PATTERN`:** filters on top of `TraceStore.page_runs`
   (which already supports name search).
4. **`stepline gc --older-than 30d`:** delete old runs, respecting forks
   (`delete_run(..., cascade_forks=True)`), with a `--dry-run` flag.
5. **"Copy request as Python" in the replay editor:** generate a `client.messages.create(...)`
   snippet from the edited request (without any key).

Bigger items are on the roadmap in the README. The **OpenAI Python SDK adapter** is the top
priority and very welcome as a contribution.

## Releasing (maintainers)

1. Update `version` in `pyproject.toml` and `__version__` in `src/stepline/__init__.py`, and
   move the CHANGELOG entry out of "Unreleased".
2. Merge to `main`, then tag and push: `git tag v0.1.0 && git push origin v0.1.0`.
3. `.github/workflows/release.yml` builds the UI and the package, checks that the tag matches
   the version, and publishes to PyPI with trusted publishing (no tokens in the repository).
