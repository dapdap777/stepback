## What and why

<!-- What does this change, and why? Link the issue if there is one (e.g. "Fixes #12"). -->

## How it was tested

<!-- Commands you ran and what you checked. New behavior needs tests. -->

- [ ] `make check` passes (lint, types, tests, oldest supported SDK)
- [ ] UI changes: `cd frontend && npm run build && npm run e2e` pass, screenshots attached
- [ ] Backend schema changes: `npm run gen:api` re-run and committed

## Checklist

- [ ] Recorder changes can't raise into user code, and keep the hot path cheap
- [ ] Recorded content is rendered as text or via `SafeMarkdown` (no raw HTML)
- [ ] No new network requests (telemetry, CDN assets); replay still needs an explicit confirmation
- [ ] Docs, README and CHANGELOG updated if behavior changed (only claim what's tested)
