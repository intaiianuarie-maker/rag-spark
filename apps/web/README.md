# apps/web — Next.js shell + status page (M0)

Frontend split of **TASK-M0-003**, implemented by `code2`. Next.js TypeScript
(strict) shell with a single status page that renders backend health from
`GET /health`, with explicit loading / error / empty / degraded states.

## Stack (pinned)

| Tool | Version | Notes |
|---|---|---|
| Node | 22 (container: `node:22-alpine`) | task floor is Node 20+ |
| Next.js | 16.4.0 | App Router, Turbopack, `output: "standalone"`, Cache Components |
| React | 19.3.0 | |
| Tailwind CSS | 4.3.x | via `@tailwindcss/turbopack` |
| TypeScript | 5.x | `strict: true` |
| ESLint | 9 flat config + `eslint-config-next` (core-web-vitals, TS) | |

No fabricated content above — exact pins live in `package.json` /
`package-lock.json` (commit the lockfile). shadcn-ready: CSS-variable layout,
`@/*` alias; `npx shadcn init` can be adopted in M1 without refactor.

## Run (local dev, no Docker)

```bash
npm install
# optional: point at a running API (default http://localhost:8000)
# export API_BASE_URL=http://localhost:8000
npm run dev            # http://localhost:3001 (dev script pins the port)
```

Verification scripts:

```bash
npm run lint           # eslint (flat config)
npm run typecheck      # tsc --noEmit (strict)
npm run build          # production build incl. TS check
npm start              # ⚠ not with standalone config — use below or dev
```

## Run (production)

```bash
npm run build
PORT=3000 HOSTNAME=0.0.0.0 node .next/standalone/server.js
# or
docker build -t knowledge-os-web:0.1.0 .
docker run -p 3000:3000 -e API_BASE_URL=http://host.docker.internal:8000 knowledge-os-web:0.1.0
```

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `API_BASE_URL` | `http://localhost:8000` | Backend origin as seen from the **web server** (server-side only). In compose: `http://api:8000`. |

No `NEXT_PUBLIC_*` variables: the browser never receives a backend URL.

## How health is fetched (and why)

```text
browser ──GET /api/health──▶ Next.js route handler ──fetch /health──▶ FastAPI
```

`src/app/api/health/route.ts` proxies `GET /health` server-side (3 s timeout,
`no-store`). This keeps the call same-origin so the page works without CORS
configuration in M0 and produces explicit error payloads:

| Backend | Proxy response to the page |
|---|---|
| 200 JSON | `200 {ok:true, fetchedAt, health:{status, version, details}}` |
| 4xx/5xx JSON | same status, `{ok:false, error:"backend_unhealthy", health?}` |
| 4xx/5xx, no JSON / non-JSON 200 | `502 {ok:false, error:"unexpected_response"}` |
| unreachable / timeout | `502 {ok:false, error:"backend_unreachable"}` |

The page (`src/components/status/health-panel.tsx`) then renders:

- **loading** — pulse skeleton while the first fetch is in flight
- **error** — red panel with HTTP/message detail + retry (auto-retry every 15 s)
- **empty** — backend answered but reported no fields at all
- **success** — status card (Operational / Degraded / Down / Unknown), version,
  all extra fields, last-checked time; polls every 15 s, pauses on hidden tabs

The payload contract is parsed defensively (`src/lib/health.ts`): unknown
backend fields are preserved and rendered, so backend changes cannot silently
break the page. No validation library is introduced in M0; contract tests for
the health payload belong to M0-004 (test1).

## Layout

```text
src/
  app/
    layout.tsx            root shell (header/footer, metadata, fonts)
    page.tsx              status page (server shell → client panel)
    api/health/route.ts   same-origin proxy to backend GET /health
  components/status/health-panel.tsx   all UI states
  lib/
    backend.ts            API_BASE_URL resolution (server-only use)
    health.ts             shared contract types + normalizer/guard
    use-health.ts         client hook: load/poll/refetch state machine
Dockerfile, .dockerignore — production image (standalone)
compose.web.yml           fragment for code1 to merge into root docker-compose.yml
.env.example              web-side env docs (code1 lifts into root .env.example)
```

## Backend contract assumed by the frontend

`GET /health` (see `docs/architecture/api-contracts.md`, implemented by
code1): HTTP 200 with a JSON object. Recognized optional fields:
`status` (string, e.g. `"ok"`), `version` (string). Anything else is displayed
as-is. If code1's shape differs, only `src/lib/health.ts` needs a review round.

## Monorepo README section (to be lifted by code1)

> ### Web UI (apps/web, port 3001)
>
> Next.js 16 shell with a live status page that renders backend `/health`.
> Run with `docker compose up` (service `web`, build context `./apps/web`) or
> locally with `npm run dev` in `apps/web` (`API_BASE_URL=http://localhost:8000`).
> Configuration: `API_BASE_URL` (server-side; no browser-exposed backend URL).
> The status page shows loading, error (backend down), empty and degraded/
> healthy states, auto-refreshing every 15 s.
