# Configuration

Stepback works with zero configuration. Everything below is optional.

## Where traces are stored

By default, traces go to `./.stepback/traces.db`, relative to the directory your agent runs
from. Stepback writes a `.gitignore` into `.stepback/` so prompts and responses are never
committed by accident.

| Setting | How |
| --- | --- |
| Database file | `STEPBACK_DB=/path/traces.db`, or `stepback.configure(db_path=...)` |
| Directory | `STEPBACK_DIR=/path/dir` (uses `<dir>/traces.db`) |
| CLI | `stepback --db /path/traces.db ls` (also reads `STEPBACK_DB`) |

Precedence: `configure(db_path=...)` > `STEPBACK_DB` > `STEPBACK_DIR` > `./.stepback`.

## Turning recording off

```bash
STEPBACK_DISABLED=1 python agent.py
```

or `stepback.configure(enabled=False)`. With recording off, the wrapped client behaves exactly
like the unwrapped one.

## Python API

```python
import stepback

stepback.configure(
    db_path="traces/dev.db",   # where to write
    run_name="nightly-eval",   # name of the default (per-process) run
    flush_timeout=5.0,         # seconds to wait for pending writes at exit
    pricing={"my-model": {"input": 1.0, "output": 2.0}},  # see below
)

with stepback.run("triage", metadata={"ticket": 4411}):
    ...  # calls in here are grouped into one named run

stepback.flush()  # rarely needed: pending writes are flushed automatically at exit
```

## Cost estimates and custom prices

Each step stores an **estimated** cost based on the model and its token usage, including
prompt-cache reads and writes, fast mode, and long-context pricing tiers. Built-in prices are
first-party Anthropic API list prices, in USD per million tokens. They don't account for batch
discounts, partner-platform (Bedrock/Vertex) pricing, or negotiated rates. If Stepback doesn't
know a model's price, it leaves the cost blank instead of guessing.

To override or extend the table, create **`.stepback/prices.toml`** next to your trace
database:

```toml
# USD per million tokens.

# Override part of a built-in entry. Fields you leave out keep their built-in values.
# Keys match exact model ids or id prefixes: "claude-opus-5-5" also covers dated ids
# such as "claude-opus-5-5-20270101".
[models."claude-opus-5-5"]
input = 3.6
output = 18.0

# Add a model Stepback doesn't know. `input` and `output` are required.
[models."my-org/finetuned-sonnet"]
input = 2.5
output = 12.0
cache_read = 0.25          # optional; defaults to 0.1 x input
```

Every field you can set:

| Field | Meaning |
| --- | --- |
| `input`, `output` | Price per million input / output tokens |
| `cache_read` | Price per million cache-read tokens (default: `0.1 × input`) |
| `tier_threshold`, `tier_input`, `tier_output` | Above `tier_threshold` prompt tokens, use the tier prices |
| `fast_input`, `fast_output` | Prices when the response reports `usage.speed == "fast"` |

Cache writes are charged at 1.25× `input` (5-minute TTL) or 2× `input` (1-hour TTL).

Notes:

- To use a price file somewhere else, set `STEPBACK_PRICES=/path/prices.toml`.
- Precedence (later wins): built-in table → `prices.toml` → `stepback.configure(pricing=...)`.
- The file is re-read whenever it changes, with no restart needed. If it can't be parsed, or has an
  unknown field, Stepback logs one warning and keeps using the built-in prices.
- Cost is computed when a step is recorded. Editing prices later doesn't change the stored cost of
  steps that were already recorded.
- `stepback prices` prints the effective table and where each entry came from.
  `stepback prices -m <model-id>` shows the price that applies to one model.

## Web UI server

```bash
stepback ui                     # http://127.0.0.1:7777 (or the next free port), opens a browser tab
stepback ui --port 8000 --no-open
stepback openapi > openapi.json # the API schema (used to generate the frontend's types)
```

The server can read every prompt you've recorded, and with replay it can spend API credits.
So it is locked to the browser tab that opened it:

- **Loopback only.** It binds to `127.0.0.1` by default. `--host` can bind somewhere else,
  but prints a warning, because that exposes your traces to anyone who can reach that address.
- **Host header check.** Only `127.0.0.1`, `localhost`, `::1` and the host you bound to are
  accepted. This defeats DNS-rebinding attacks, where another site points its own hostname
  at your machine.
- **Same-origin only, no CORS.** A request that carries an `Origin` header must come from the
  UI's own origin. The server never sends `Access-Control-Allow-*` headers, so pages on other
  sites (or other local ports) can't read its responses.
- **State-changing endpoints** (delete, and replay later on) use POST or DELETE only. They also
  require an `X-Stepback-Client: 1` header, which a cross-origin page can't send without a CORS
  preflight that is never approved. If the browser sends `Sec-Fetch-Site`, it must be
  `same-origin`. A script on the same machine can still call these endpoints with
  `curl -X DELETE -H "X-Stepback-Client: 1" ...`.
- Responses set `nosniff`, `X-Frame-Options: DENY`, a strict Content-Security-Policy, and
  `Cache-Control: no-store` on the API. The interactive Swagger/ReDoc pages are turned off
  because they load scripts from a CDN. The schema is still served at `/api/openapi.json`.

When working on the frontend with Vite's dev server, allow its exact origin:
`stepback ui --allow-origin http://localhost:5173` (or `STEPBACK_ALLOW_ORIGINS`).
Wildcards are rejected.

## Replay

Select a step in the UI and press **Replay** (or `r`). You can edit the model, system prompt,
messages, tools and parameters, then re-send that single call. The result is saved as a **new
forked run** linked to the original step.

- **Nothing is sent until you confirm.** *Review cost* validates your edits and shows the
  model, input tokens and estimated cost range (input cost, plus output up to `max_tokens`).
  Counting tokens uses the API's free `count_tokens` endpoint, falling back to a local
  estimate if that fails. The paid request goes out only when you click **Send replay**. It is
  exactly one request: no automatic retries, no batch replay.
- **Your key stays in your environment.** The key is read from the environment of the
  `stepback ui` process at the moment you replay (`ANTHROPIC_API_KEY`, or
  `ANTHROPIC_AUTH_TOKEN`, or an `ant auth login` profile with recent SDKs). It is never
  stored, logged, or returned by the API. If no key is set, the UI tells you how to set one:

  ```bash
  export ANTHROPIC_API_KEY=sk-ant-...
  stepback ui
  ```

- **History is immutable.** The original run is never modified. Each replay creates a new run
  (`parent_run_id` = the original run, `forked_from_step_id` = the replayed step) that inherits
  the steps before it.
- **One call, not your agent.** Replay doesn't re-run your tool code, so the fork ends at the
  replayed step, even if the new response asks for a tool.
- **Failures are kept.** If the API rejects the replay (authentication, rate limit, invalid
  request, retired model...), the fork is still saved, with a failed step showing the API's own
  error message.
- You can change the model, which helps when the recorded one has been retired. Replays are
  sent without streaming, and `stream` is ignored.
- **Try it offline:** `stepback ui --mock` sends replays to the bundled mock API instead of
  Anthropic. No key is needed, and nothing is sent or billed. The mock is scripted for the
  example agent (`stepback demo`), so its answers are canned.

Compare a fork with its parent from the fork's page (**Compare with parent**, or `d`).

## Supported SDKs

| SDK | Versions | Clients |
| --- | --- | --- |
| `anthropic` (Python) | `>=0.40, <3` | `Anthropic`, `AsyncAnthropic`, and the Bedrock / Vertex / Foundry clients |

Recorded methods are `messages.create` (including `stream=True`), `messages.parse`, and
`messages.stream` on both `client.messages` and `client.beta.messages`. CI tests the oldest and
the newest supported SDK release. With a version outside the range, Stepback logs a warning
and still tries to record. If the SDK's internals have changed, steps may be saved without a
response body. Your application is never affected.

## Logging

Stepback logs to the `stepback` logger. Internal problems are logged as one warning each,
then at debug level, and never raise into your code:

```python
import logging
logging.getLogger("stepback").setLevel(logging.DEBUG)
```
