# Stepback UI

React + Vite + Tailwind. The production build is written into
`src/stepback/server/static/` and ships inside the Python wheel. Everything is bundled
(fonts, icons, code): the UI makes no requests except to its own server.

```bash
npm ci
npm run build          # typecheck + build into the Python package
npm run gen:api        # regenerate src/api/schema.d.ts from the backend's OpenAPI schema
npm run e2e            # Playwright: seeds a database, starts `stepback ui`, runs e2e/*.spec.ts
```

Live development: run `stepback ui --no-open` (port 7777) in one terminal and `npm run dev` in
another, then open http://localhost:5173. Vite proxies `/api` to the backend with the original
Host header, so the backend's same-origin checks pass without extra flags.

Rules for recorded content (prompts, model output, tool I/O): it is untrusted. Render it as
React text, or as markdown through `components/SafeMarkdown.tsx`. Never use
`dangerouslySetInnerHTML`. The server's CSP enforces Trusted Types, so any HTML-string sink
throws at runtime, and the e2e XSS test fails.
