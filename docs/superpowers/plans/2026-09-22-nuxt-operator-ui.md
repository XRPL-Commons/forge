# Nuxt Operator UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the hand-written Forge operator pages with a server-rendered Nuxt 4 app at `ui/` that fronts the unchanged Python JSON API through a guarded Nitro proxy.

**Architecture:** Nitro binds the tailnet address and serves every page; a single proxy handler forwards `/api/**` and the GitHub callback to the Python status server on `127.0.0.1:8788`, rewriting `Host`/`Origin` and enforcing the same-origin action guard at the edge. Pages are Vue components built with Nuxt UI v4; one polling composable reproduces the current refresh rules; Python stays the only writer.

**Tech Stack:** Nuxt 4.5 (Node 24 on the host, Node 22 in the runner image), Nuxt UI 4.11 + Tailwind 4, TypeScript, Vitest, Playwright (`playwright-core` + `/usr/bin/chromium` inside `forge-runner:local`), markdown-it + sanitize-html for Help.

**Spec:** `docs/superpowers/specs/2026-09-22-nuxt-operator-ui-design.md`. **Contracts:** `docs/superpowers/specs/2026-09-22-operator-api-contracts.md` (every payload shape, action value, user-facing string and polling rule; read the section for a screen before building it).

## Global Constraints

- App lives in `ui/` of XRPL-Commons/forge; nothing under `forge/`, `deploy/runner/` or the runner image changes except the cleanup task.
- Nitro binds `HOST`/`PORT` from the environment (`<tailscale-ip>:8787` in production); Python is reached only at `NITRO_FORGE_API` (`http://127.0.0.1:8788`).
- Every POST to Python passes the edge guard: `Origin === 'http://' + Host`, `Sec-Fetch-Site === 'same-origin'`, `Content-Type` starts with `application/json`, `X-Forge-Action` present and non-empty, body ≤ 262144 bytes. Nitro never stores the token.
- Proxy timeout 30 s, response body limit 1 MiB, only these response headers pass through: `content-type`, `set-cookie`, `location`, `cache-control`, `content-disposition` (the last one added in Task 9 so the ruleset download keeps its filename).
- Dark theme only; tokens: bg `#101416`, panel `#192024`, line `#303b40`, text `#edf1ee`, muted `#acb7b9`, primary mint `#bfe6cd`, blue `#abcfff`, amber `#ecc486`, red `#f0a6a6`.
- Every `<select>` is `USelectMenu`; banners are `UAlert`; confirmations are `UModal`. No page-level custom CSS.
- User-facing strings are copied from the contracts document verbatim unless a task says otherwise.
- Polling rules (from the contracts document): default interval 5 s, failure backoff to 15 s, paused while `document.hidden`, coalesced in-flight requests, last good payload retained with a stale indicator. The interval is read from `runtimeConfig.public.pollInterval` (default 5000, overridden by `NUXT_PUBLIC_POLL_INTERVAL`), never from a literal in a page.
- Every poll that can land while the operator is mid-action passes `usePoll`'s `allowed?: () => boolean` guard (added in Task 5): Activity `() => !confirming`, Issues `() => !submitting`, Settings `() => !saving && !dirty`, Repository setup `() => !busy`, Onboarding the contracts §4 predicate. When `allowed()` is false the load is skipped and rescheduled without touching `data`, `error` or `stale`.
- Every read passes a fetch timeout — `get(path, { timeout })` → `AbortSignal.timeout(timeout ?? 15000)` (added in Task 5): 15 s Activity, 30 s Issues / Settings / Repository setup / Onboarding, 10 s header pill (contracts §1 "Client conventions").
- UI-owned server routes live under `ui/server/routes/_ui/` and are called at `/_ui/<name>` (`/_ui/markdown`, `/_ui/help`, `/_ui/setup-cookie`). Nitro auto-maps only `server/api/**` and `server/routes/**`, and the `_ui` prefix stays clear of `proxyPaths()`, which matches `/api` and `/api/*`.
- Page titles are `useHead({ title: 'X' })` rendered through `titleTemplate: '%s · Forge'` — "Activity · Forge", "Issues · Forge", "Settings · Forge", "GitHub access · Forge", "Prepare repository · Forge", "Coding accounts · Forge", "Setup · Forge", "Reset Forge · Forge", "Setup help · Forge". The contracts' "Forge · X" titles are deliberately replaced by these tab names.
- At most one message block is rendered above the metrics on any page: connection lost → the freshness pill only; otherwise the warning banner(s); otherwise the operator status line. The same sentence is never printed twice.
- `warnings()` (Task 6) is the single producer of banner objects, reused by Task 8; no component constructs a warning object inline, and a warning never links to the route it is displayed on.
- Worker output stays reachable for any job — running, finished, recovery or orphaned cleanup — via `/issues/<key>?job=<id>`; no surface may be the only route to a job's logs.
- e2e fixture naming: path segments after `/api/` join with `-` (`factory-output.json`), a query variant appends `.<param>` (`issues.issue.json`, `repositories.repo_id.json`), a POST body fixture appends `.post.json` (`runtime.post.json`, `repositories-enable.post.json`).
- Recovery-role workers count as working wherever counts are shown.
- Tests run on the box: unit with `npm test`; e2e with `npm run test:e2e` inside the runner image (see Task 5). CI mirrors both.
- Commit after every task with a signed commit (git config on the box signs automatically); message style: imperative sentence, no prefix.

---

### Task 1: Scaffold `ui/` with Nuxt 4, Nuxt UI v4 and the theme

**Files:**
- Create: `ui/package.json`, `ui/nuxt.config.ts`, `ui/tsconfig.json`, `ui/app/app.vue`, `ui/app/assets/css/main.css`, `ui/app/app.config.ts`, `ui/public/favicon.svg`, `ui/.gitignore`, `ui/vitest.config.ts`
- Modify: `.github/workflows/ci.yml` (add a `ui` job; see Step 8)

**Interfaces:**
- Produces: a runnable app (`npm run dev`, `npm run build`), `npm test` (Vitest), theme tokens available as Tailwind colors `bg`, `panel`, `line`, `muted`, `mint`, `sky`, `amber`, `rose`.

- [ ] **Step 1: Create the project**

```bash
cd ~/forger-remote && mkdir ui && cd ui
npm init -y >/dev/null
npm install nuxt@^4.5 @nuxt/ui@^4.11 vue vue-router
npm install -D typescript vitest @vitest/coverage-v8 @nuxt/test-utils happy-dom playwright-core@1.55.0
```

- [ ] **Step 2: Write `ui/package.json` scripts**

```json
{
  "name": "forge-ui",
  "private": true,
  "type": "module",
  "scripts": {
    "dev": "nuxt dev",
    "build": "nuxt build",
    "preview": "node .output/server/index.mjs",
    "test": "vitest run",
    "test:e2e": "node tests/e2e/run.mjs"
  }
}
```
(keep the `dependencies`/`devDependencies` npm wrote.)

- [ ] **Step 3: Write `ui/nuxt.config.ts`**

```ts
export default defineNuxtConfig({
  compatibilityDate: '2026-09-01',
  modules: ['@nuxt/ui'],
  css: ['~/assets/css/main.css'],
  ssr: true,
  devtools: { enabled: false },
  runtimeConfig: {
    forgeApi: 'http://127.0.0.1:8788',   // NITRO_FORGE_API overrides
    helpDoc: '../docs/onboarding.md',    // NITRO_HELP_DOC overrides
  },
  app: {
    head: {
      titleTemplate: '%s · Forge',
      htmlAttrs: { lang: 'en', class: 'dark' },
      link: [{ rel: 'icon', type: 'image/svg+xml', href: '/favicon.svg' }],
    },
  },
  colorMode: { preference: 'dark', fallback: 'dark', classSuffix: '' },
  nitro: { compressPublicAssets: false },
})
```

- [ ] **Step 4: Write the theme `ui/app/assets/css/main.css`**

```css
@import "tailwindcss";
@import "@nuxt/ui";

@theme static {
  --color-bg: #101416;
  --color-panel: #192024;
  --color-line: #303b40;
  --color-muted: #acb7b9;
  --color-mint-50: #f1faf4; --color-mint-100: #dff3e6; --color-mint-200: #bfe6cd; --color-mint-300: #9fd9b5;
  --color-mint-400: #7fcc9d; --color-mint-500: #5fbf85; --color-mint-600: #3f9f66; --color-mint-700: #2f7f50;
  --color-mint-800: #205f3a; --color-mint-900: #143f26; --color-mint-950: #0a2013;
  --color-sky-200: #abcfff;
  --color-amber-200: #ecc486;
  --color-rose-200: #f0a6a6;
}

:root { --ui-bg: var(--color-bg); --ui-bg-elevated: var(--color-panel); --ui-border: var(--color-line);
        --ui-text: #edf1ee; --ui-text-muted: var(--color-muted); --ui-text-dimmed: var(--color-muted); }
body { background: var(--color-bg); color: #edf1ee; font: 15px/1.55 system-ui, -apple-system, sans-serif; }
```

- [ ] **Step 5: Write `ui/app/app.config.ts`**

```ts
export default defineAppConfig({
  ui: {
    colors: { primary: 'mint', neutral: 'zinc' },
    card: { slots: { root: 'bg-panel ring-line', body: 'p-4 sm:p-4' } },
    button: { slots: { base: 'rounded-full' } },
  },
})
```

- [ ] **Step 6: Write `ui/app/app.vue` and copy the favicon**

```vue
<template>
  <UApp>
    <NuxtLayout>
      <NuxtPage />
    </NuxtLayout>
  </UApp>
</template>
```

Copy the SVG string from `FAVICON` in `forge/navigation.py` (the `<svg …>…</svg>` literal) into `ui/public/favicon.svg`.

- [ ] **Step 7: Write `ui/vitest.config.ts` and a smoke test**

```ts
import { defineVitestConfig } from '@nuxt/test-utils/config'
export default defineVitestConfig({ test: { environment: 'happy-dom', include: ['tests/unit/**/*.test.ts'] } })
```

`ui/tests/unit/theme.test.ts`:
```ts
import { readFileSync } from 'node:fs'
import { describe, it, expect } from 'vitest'
describe('theme', () => {
  it('declares the Forge tokens', () => {
    const css = readFileSync(new URL('../../app/assets/css/main.css', import.meta.url), 'utf8')
    for (const hex of ['#101416', '#192024', '#303b40', '#bfe6cd', '#abcfff', '#ecc486']) expect(css).toContain(hex)
  })
})
```

Run: `cd ui && npm test` → PASS. Run: `npm run build` → `.output/` exists. Run: `PORT=3999 HOST=127.0.0.1 node .output/server/index.mjs &` then `curl -s http://127.0.0.1:3999/ | grep -c '<div id="__nuxt"'` → `1`; kill the server.

- [ ] **Step 8: Add the CI job**

Append to `.github/workflows/ci.yml` under `jobs:`:
```yaml
  ui:
    runs-on: ubuntu-latest
    defaults: { run: { working-directory: ui } }
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with: { node-version: 24, cache: npm, cache-dependency-path: ui/package-lock.json }
      - run: npm ci
      - run: npm test
      - run: npm run build
```
(e2e is added to this job in Task 5.)

- [ ] **Step 9: `ui/.gitignore`**

```
node_modules
.nuxt
.output
.data
```

- [ ] **Step 10: Commit**

```bash
git add ui .github/workflows/ci.yml
git commit -m "Scaffold the Nuxt operator UI with the Forge theme"
```

---

### Task 2: Guarded proxy to the Python API

**Files:**
- Create: `ui/server/utils/proxy.ts`, `ui/server/middleware/forge-proxy.ts`
- Test: `ui/tests/unit/proxy.test.ts`

**Interfaces:**
- Produces: `guardAction(req: { method: string; headers: Record<string,string|undefined>; contentLength: number }): { ok: true } | { ok: false; status: number; reason: string }` and `proxyPaths: (path: string) => boolean` in `ui/server/utils/proxy.ts`; the middleware forwards matching requests and short-circuits others.

- [ ] **Step 1: Write the failing tests `ui/tests/unit/proxy.test.ts`**

```ts
import { describe, it, expect } from 'vitest'
import { guardAction, proxyPaths, upstreamHeaders } from '../../server/utils/proxy'

const base = { method: 'POST', contentLength: 100, headers: {
  host: '100.111.151.38:8787', origin: 'http://100.111.151.38:8787', 'sec-fetch-site': 'same-origin',
  'content-type': 'application/json', 'x-forge-action': 'tok' } }

describe('guardAction', () => {
  it('accepts a same-origin JSON action with the header', () => expect(guardAction(base)).toEqual({ ok: true }))
  it('lets GET through untouched', () => expect(guardAction({ ...base, method: 'GET', headers: {} })).toEqual({ ok: true }))
  it('rejects a foreign origin', () => expect(guardAction({ ...base, headers: { ...base.headers, origin: 'http://evil:8787' } })).toMatchObject({ ok: false, status: 403 }))
  it('rejects cross-site fetch metadata', () => expect(guardAction({ ...base, headers: { ...base.headers, 'sec-fetch-site': 'cross-site' } })).toMatchObject({ ok: false, status: 403 }))
  it('rejects a missing action header', () => expect(guardAction({ ...base, headers: { ...base.headers, 'x-forge-action': '' } })).toMatchObject({ ok: false, status: 403 }))
  it('rejects non-JSON bodies', () => expect(guardAction({ ...base, headers: { ...base.headers, 'content-type': 'text/plain' } })).toMatchObject({ ok: false, status: 415 }))
  it('rejects oversized bodies', () => expect(guardAction({ ...base, contentLength: 262145 })).toMatchObject({ ok: false, status: 413 }))
})

describe('proxyPaths', () => {
  it('matches the API and the GitHub callback only', () => {
    expect(proxyPaths('/api/factory')).toBe(true)
    expect(proxyPaths('/api/issues?state=open')).toBe(true)
    expect(proxyPaths('/onboarding/github/callback')).toBe(true)
    expect(proxyPaths('/issues')).toBe(false)
    expect(proxyPaths('/favicon.svg')).toBe(false)
  })
})

describe('upstreamHeaders', () => {
  it('rewrites host and origin and forwards cookies and the action header', () => {
    const out = upstreamHeaders(base.headers, 'POST', '127.0.0.1:8788')
    expect(out.host).toBe('127.0.0.1:8788')
    expect(out.origin).toBe('http://127.0.0.1:8788')
    expect(out['sec-fetch-site']).toBe('same-origin')
    expect(out['x-forge-action']).toBe('tok')
    expect(out['content-type']).toBe('application/json')
  })
  it('does not invent an origin on GET', () => {
    const out = upstreamHeaders({ host: 'x', cookie: 'forge_setup=abc' }, 'GET', '127.0.0.1:8788')
    expect(out.origin).toBeUndefined(); expect(out.cookie).toBe('forge_setup=abc')
  })
})
```

- [ ] **Step 2: Run to verify failure**

Run: `cd ui && npx vitest run tests/unit/proxy.test.ts` → FAIL (module not found).

- [ ] **Step 3: Implement `ui/server/utils/proxy.ts`**

```ts
export const MAX_ACTION_BODY = 262144
export const MAX_RESPONSE_BODY = 1024 * 1024
export const UPSTREAM_TIMEOUT_MS = 30000
const PASS_RESPONSE = ['content-type', 'set-cookie', 'location', 'cache-control']

type Headers = Record<string, string | undefined>
export interface GuardInput { method: string; headers: Headers; contentLength: number }
export type GuardResult = { ok: true } | { ok: false; status: number; reason: string }

export function proxyPaths(path: string): boolean {
  const clean = path.split('?')[0]
  return clean === '/api' || clean.startsWith('/api/') || clean === '/onboarding/github/callback'
}

export function guardAction({ method, headers, contentLength }: GuardInput): GuardResult {
  if (method !== 'POST') return { ok: true }
  const host = headers.host ?? ''
  if (!host || headers.origin !== 'http://' + host) return { ok: false, status: 403, reason: 'Cross-origin action rejected' }
  if (headers['sec-fetch-site'] !== 'same-origin') return { ok: false, status: 403, reason: 'Cross-site action rejected' }
  if (!headers['x-forge-action']) return { ok: false, status: 403, reason: 'Action token missing' }
  if (!(headers['content-type'] ?? '').split(';')[0].trim().startsWith('application/json')) return { ok: false, status: 415, reason: 'Actions are JSON' }
  if (!(contentLength > 0 && contentLength <= MAX_ACTION_BODY)) return { ok: false, status: 413, reason: 'Action body too large' }
  return { ok: true }
}

export function upstreamHeaders(headers: Headers, method: string, upstreamHost: string): Headers {
  const out: Headers = { host: upstreamHost }
  for (const name of ['cookie', 'content-type', 'x-forge-action', 'accept']) if (headers[name]) out[name] = headers[name]
  if (method === 'POST') { out.origin = 'http://' + upstreamHost; out['sec-fetch-site'] = 'same-origin' }
  return out
}

export function responseHeaders(from: globalThis.Headers): [string, string][] {
  const out: [string, string][] = []
  from.forEach((value, name) => { if (PASS_RESPONSE.includes(name.toLowerCase())) out.push([name, value]) })
  return out
}
```

- [ ] **Step 4: Implement the middleware `ui/server/middleware/forge-proxy.ts`**

```ts
import { guardAction, proxyPaths, upstreamHeaders, responseHeaders, MAX_RESPONSE_BODY, UPSTREAM_TIMEOUT_MS } from '../utils/proxy'

export default defineEventHandler(async (event) => {
  const path = event.path
  if (!proxyPaths(path)) return
  const req = event.node.req
  const headers = Object.fromEntries(Object.entries(req.headers).map(([k, v]) => [k, Array.isArray(v) ? v.join(',') : v]))
  const guard = guardAction({ method: req.method ?? 'GET', headers, contentLength: Number(headers['content-length'] ?? 0) })
  if (!guard.ok) { setResponseStatus(event, guard.status); return { ok: false, reason: guard.reason } }
  const base = useRuntimeConfig().forgeApi.replace(/\/$/, '')
  const upstreamHost = new URL(base).host
  const body = req.method === 'POST' ? await readRawBody(event, false) : undefined
  const controller = new AbortController(); const timer = setTimeout(() => controller.abort(), UPSTREAM_TIMEOUT_MS)
  try {
    const upstream = await fetch(base + path, { method: req.method, headers: upstreamHeaders(headers, req.method ?? 'GET', upstreamHost) as Record<string, string>,
      body, redirect: 'manual', signal: controller.signal })
    const buffer = Buffer.from(await upstream.arrayBuffer())
    if (buffer.length > MAX_RESPONSE_BODY) { setResponseStatus(event, 502); return { ok: false, reason: 'Upstream response too large' } }
    setResponseStatus(event, upstream.status)
    for (const [name, value] of responseHeaders(upstream.headers)) appendResponseHeader(event, name, value)
    return buffer
  } catch (error) {
    setResponseStatus(event, 502)
    return { ok: false, reason: 'Forge status service is unavailable. Check forge-status on the server.' }
  } finally { clearTimeout(timer) }
})
```

- [ ] **Step 5: Run the unit tests** → PASS. Then a live check against a stub: start a Python stub that echoes headers on `127.0.0.1:8788`:
```bash
python3 - <<'EOF' &
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
        self.wfile.write(json.dumps({'host': self.headers['Host'], 'path': self.path}).encode())
    def do_POST(self):
        self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
        self.wfile.write(json.dumps({'origin': self.headers['Origin'], 'sfs': self.headers['Sec-Fetch-Site'], 'tok': self.headers['X-Forge-Action']}).encode())
HTTPServer(('127.0.0.1', 8788), H).serve_forever()
EOF
cd ui && npm run build && PORT=3999 HOST=127.0.0.1 node .output/server/index.mjs & sleep 2
curl -s http://127.0.0.1:3999/api/factory                       # → {"host":"127.0.0.1:8788","path":"/api/factory"}
curl -s -X POST http://127.0.0.1:3999/api/factory/action -H 'Origin: http://127.0.0.1:3999' -H 'Sec-Fetch-Site: same-origin' -H 'Content-Type: application/json' -H 'X-Forge-Action: t' -d '{}'   # → origin 127.0.0.1:8788, tok t
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:3999/api/factory/action -H 'Content-Type: application/json' -d '{}'   # → 403
kill %1 %2
```

- [ ] **Step 6: Commit**

```bash
git add ui/server ui/tests/unit/proxy.test.ts
git commit -m "Proxy the Python API through a same-origin guard"
```

---

### Task 3: API client and polling composables

**Files:**
- Create: `ui/app/composables/useForgeApi.ts`, `ui/app/composables/usePoll.ts`, `ui/app/utils/format.ts`
- Test: `ui/tests/unit/usePoll.test.ts`, `ui/tests/unit/format.test.ts`

**Interfaces:**
- Produces:
  - `useForgeApi()` → `{ get<T>(path: string): Promise<T>; act<T>(path: string, body: object): Promise<T>; token: Ref<string> }`. `get` records `payload.controls_token` into `token` when present; `act` sends `X-Forge-Action: token.value` with `Content-Type: application/json` and throws `ForgeApiError(status, reason)` when the response is not 2xx or `ok === false`.
  - `usePoll<T>(load: () => Promise<T>, options?: { interval?: number; onChange?: (value: T) => void })` → `{ data: Ref<T | null>; error: Ref<string | null>; stale: Ref<boolean>; refresh(): Promise<T | null>; start(): void; stop(): void }` with the rules from Global Constraints.
  - `formatAge(seconds: number): string` ("just now", "2 min", "3 h", "2 d"), `formatDuration(seconds: number): string` ("1 min", "1 h 05 min"), `clampTitle(text: string, max = 96): string`.

- [ ] **Step 1: Write the failing tests**

`ui/tests/unit/usePoll.test.ts`:
```ts
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { usePoll } from '../../app/composables/usePoll'

describe('usePoll', () => {
  beforeEach(() => { vi.useFakeTimers(); Object.defineProperty(document, 'hidden', { value: false, configurable: true }) })
  afterEach(() => vi.useRealTimers())

  it('coalesces overlapping refreshes and keeps the last payload on failure', async () => {
    let calls = 0
    const load = vi.fn(async () => { calls++; if (calls === 2) throw new Error('down'); return { n: calls } })
    const poll = usePoll(load, { interval: 5000 })
    const a = poll.refresh(); const b = poll.refresh(); await Promise.all([a, b])
    expect(load).toHaveBeenCalledTimes(1); expect(poll.data.value).toEqual({ n: 1 }); expect(poll.stale.value).toBe(false)
    await poll.refresh()
    expect(poll.data.value).toEqual({ n: 1 }); expect(poll.stale.value).toBe(true); expect(poll.error.value).toBe('down')
  })

  it('backs off to 15 s after a failure and resumes 5 s on success', async () => {
    let fail = true
    const load = vi.fn(async () => { if (fail) throw new Error('x'); return 1 })
    const poll = usePoll(load, { interval: 5000 }); poll.start(); await vi.advanceTimersByTimeAsync(0)
    expect(load).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(5000); expect(load).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(10000); expect(load).toHaveBeenCalledTimes(2)
    fail = false; await vi.advanceTimersByTimeAsync(15000); expect(load).toHaveBeenCalledTimes(3)
    await vi.advanceTimersByTimeAsync(5000); expect(load).toHaveBeenCalledTimes(4)
    poll.stop()
  })

  it('does not poll while the tab is hidden and refreshes when it becomes visible', async () => {
    Object.defineProperty(document, 'hidden', { value: true, configurable: true })
    const load = vi.fn(async () => 1)
    const poll = usePoll(load, { interval: 5000 }); poll.start(); await vi.advanceTimersByTimeAsync(20000)
    expect(load).not.toHaveBeenCalled()
    Object.defineProperty(document, 'hidden', { value: false, configurable: true })
    document.dispatchEvent(new Event('visibilitychange')); await vi.advanceTimersByTimeAsync(0)
    expect(load).toHaveBeenCalledTimes(1); poll.stop()
  })

  it('calls onChange only when the payload changed', async () => {
    const onChange = vi.fn(); const load = vi.fn(async () => ({ same: true }))
    const poll = usePoll(load, { onChange }); await poll.refresh(); await poll.refresh()
    expect(onChange).toHaveBeenCalledTimes(1)
  })
})
```

`ui/tests/unit/format.test.ts`:
```ts
import { describe, it, expect } from 'vitest'
import { formatAge, formatDuration, clampTitle } from '../../app/utils/format'
describe('format', () => {
  it('ages', () => { expect(formatAge(5)).toBe('just now'); expect(formatAge(130)).toBe('2 min'); expect(formatAge(7200)).toBe('2 h'); expect(formatAge(200000)).toBe('2 d') })
  it('durations', () => { expect(formatDuration(59)).toBe('less than a minute'); expect(formatDuration(60)).toBe('1 min'); expect(formatDuration(3900)).toBe('1 h 05 min') })
  it('clamps titles at a word boundary with an ellipsis', () => {
    expect(clampTitle('short')).toBe('short')
    const long = 'word '.repeat(40).trim(); const out = clampTitle(long, 30)
    expect(out.length).toBeLessThanOrEqual(31); expect(out.endsWith('…')).toBe(true)
  })
})
```

- [ ] **Step 2: Run to verify failure** — `npx vitest run tests/unit/usePoll.test.ts tests/unit/format.test.ts` → FAIL.

- [ ] **Step 3: Implement `ui/app/utils/format.ts`**

```ts
export function formatAge(seconds: number): string {
  if (seconds < 60) return 'just now'
  if (seconds < 3600) return Math.floor(seconds / 60) + ' min'
  if (seconds < 86400) return Math.floor(seconds / 3600) + ' h'
  return Math.floor(seconds / 86400) + ' d'
}
export function formatDuration(seconds: number): string {
  if (seconds < 60) return 'less than a minute'
  const h = Math.floor(seconds / 3600), m = Math.floor((seconds % 3600) / 60)
  return h ? `${h} h ${String(m).padStart(2, '0')} min` : `${m} min`
}
export function clampTitle(text: string, max = 96): string {
  if (text.length <= max) return text
  const cut = text.slice(0, max); const space = cut.lastIndexOf(' ')
  return (space > max * 0.6 ? cut.slice(0, space) : cut).trimEnd() + '…'
}
```

- [ ] **Step 4: Implement `ui/app/composables/useForgeApi.ts`**

```ts
export class ForgeApiError extends Error { constructor(public status: number, message: string) { super(message) } }
const token = () => useState<string>('forge-action-token', () => '')

export function useForgeApi() {
  const current = token()
  async function get<T>(path: string): Promise<T> {
    const response = await $fetch.raw<T & { ok?: boolean; reason?: string; controls_token?: string }>(path, { method: 'GET', credentials: 'same-origin' })
    const payload = response._data as any
    if (!response.ok || (payload && payload.ok === false)) throw new ForgeApiError(response.status, payload?.reason ?? 'Request failed')
    if (payload?.controls_token) current.value = payload.controls_token
    return payload as T
  }
  async function act<T>(path: string, body: object): Promise<T> {
    let response
    try {
      response = await $fetch.raw<T & { ok?: boolean; reason?: string }>(path, { method: 'POST', body, credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-Forge-Action': current.value } })
    } catch (error: any) {
      const status = error?.response?.status ?? 0, reason = error?.response?._data?.reason
      throw new ForgeApiError(status, reason ?? (status === 403 ? 'This page is out of date. Refresh and try again.' : 'Forge did not accept the action.'))
    }
    const payload = response._data as any
    if (payload && payload.ok === false) throw new ForgeApiError(response.status, payload.reason ?? 'Forge did not accept the action.')
    return payload as T
  }
  return { get, act, token: current }
}
```

- [ ] **Step 5: Implement `ui/app/composables/usePoll.ts`**

```ts
import { ref, onScopeDispose, getCurrentScope } from 'vue'
export interface PollOptions<T> { interval?: number; onChange?: (value: T) => void }
export function usePoll<T>(load: () => Promise<T>, options: PollOptions<T> = {}) {
  const interval = options.interval ?? 5000
  const data = ref<T | null>(null) as import('vue').Ref<T | null>
  const error = ref<string | null>(null); const stale = ref(false)
  let timer: ReturnType<typeof setTimeout> | undefined, pending: Promise<T | null> | null = null, signature = '', running = false
  const hidden = () => typeof document !== 'undefined' && document.hidden
  function schedule(delay = interval) { clearTimeout(timer); if (running && !hidden()) timer = setTimeout(() => { void refresh() }, delay) }
  function refresh(): Promise<T | null> {
    if (pending) return pending
    pending = (async () => {
      try {
        const next = await load(); const key = JSON.stringify(next)
        data.value = next; error.value = null; stale.value = false
        if (key !== signature) { signature = key; options.onChange?.(next) }
        schedule(); return next
      } catch (failure: any) {
        error.value = failure?.message ?? 'Unavailable'; stale.value = data.value !== null; schedule(Math.max(interval, 15000)); return data.value
      } finally { pending = null }
    })()
    return pending
  }
  function onVisibility() { clearTimeout(timer); if (!hidden() && running) void refresh() }
  function start() { if (running) return; running = true; document.addEventListener('visibilitychange', onVisibility); if (!hidden()) void refresh() }
  function stop() { running = false; clearTimeout(timer); document.removeEventListener('visibilitychange', onVisibility) }
  if (getCurrentScope()) onScopeDispose(stop)
  return { data, error, stale, refresh, start, stop }
}
```

- [ ] **Step 6: Run tests** → PASS. Commit:

```bash
git add ui/app/composables ui/app/utils ui/tests/unit
git commit -m "Add the Forge API client and the polling composable"
```

---

### Task 4: Layout, navigation and the live activity pill

**Files:**
- Create: `ui/app/layouts/default.vue`, `ui/app/components/AppHeader.vue`, `ui/app/components/ActivityPill.vue`, `ui/app/components/WorkTabs.vue`, `ui/app/components/SettingsTabs.vue`, `ui/app/utils/activity.ts`
- Test: `ui/tests/unit/activity.test.ts`

**Interfaces:**
- Consumes: `usePoll`, `useForgeApi`, the `/api/factory` shape from the contracts document (section "GET /api/factory": `active`, `observation.at`, `runtime.cycle_seconds`, `jobs[]` with `phase`, `role`, `cleanup`, `fenced`, `outcome`, `observed_at`/`last_observed`, `issues[]` with `active`, `state`, `repo`, `repositories[]` with `enabled`, `name`).
- Produces: `summarizeActivity(payload, now): { working: Record<'build'|'review'|'audit'|'merge'|'recovery', number>; queued: number; fresh: boolean }` in `ui/app/utils/activity.ts`; layout with slots for a page eyebrow/title.

- [ ] **Step 1: Failing test `ui/tests/unit/activity.test.ts`**

```ts
import { describe, it, expect } from 'vitest'
import { summarizeActivity } from '../../app/utils/activity'
const now = 1_000_000
const payload = { ok: true, active: true, observation: { at: now - 10 }, runtime: { cycle_seconds: 30, enabled: true },
  jobs: [ { role: 'build', phase: 'running', observed_at: now - 5 }, { role: 'recovery', phase: 'running', observed_at: now - 5 },
          { role: 'review', phase: 'running', observed_at: now - 1000 }, { role: 'audit', phase: 'running', cleanup: true, observed_at: now - 5 } ],
  issues: [ { state: 'ready', active: null, repo: 'a/b' }, { state: 'built', active: 'j', repo: 'a/b' }, { state: 'ready', active: null, repo: 'a/off' } ],
  repositories: [ { name: 'a/b', enabled: true }, { name: 'a/off', enabled: false } ] }
describe('summarizeActivity', () => {
  it('counts fresh running workers per role, recovery included, and queued issues of enabled repos', () => {
    expect(summarizeActivity(payload as any, now)).toEqual({ working: { build: 1, review: 0, audit: 0, merge: 0, recovery: 1 }, queued: 1, fresh: true })
  })
  it('reports stale when the observation is older than three cycles', () => {
    expect(summarizeActivity({ ...payload, observation: { at: now - 200 } } as any, now).fresh).toBe(false)
  })
})
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement `ui/app/utils/activity.ts`**

```ts
export type Role = 'build' | 'review' | 'audit' | 'merge' | 'recovery'
export const ROLE_LABELS: Record<Role, [string, string]> = { build: ['builder', 'builders'], review: ['reviewer', 'reviewers'], audit: ['auditor', 'auditors'], merge: ['merge worker', 'merge workers'], recovery: ['investigation', 'investigations'] }
export function summarizeActivity(payload: any, now: number) {
  const maxAge = Math.max(120, (Number(payload?.runtime?.cycle_seconds) || 30) * 3)
  const fresh = !!payload?.observation?.at && now - payload.observation.at <= maxAge
  const working: Record<Role, number> = { build: 0, review: 0, audit: 0, merge: 0, recovery: 0 }
  for (const job of payload?.jobs ?? []) {
    const seen = job.observed_at ?? job.last_observed
    if (job.cleanup || job.fenced || job.outcome || job.phase !== 'running' || !seen || now - seen > maxAge) continue
    if (job.role in working) working[job.role as Role]++
  }
  const enabled = new Set((payload?.repositories ?? []).filter((r: any) => r.enabled).map((r: any) => r.name))
  const queued = (payload?.issues ?? []).filter((i: any) => !i.active && ['ready', 'built', 'reviewed'].includes(i.state) && (!payload.repositories || enabled.has(i.repo))).length
  return { working, queued, fresh }
}
```

- [ ] **Step 4: Components.** `ui/app/components/ActivityPill.vue`:

```vue
<script setup lang="ts">
import { summarizeActivity, ROLE_LABELS, type Role } from '~/utils/activity'
const api = useForgeApi()
const poll = usePoll(() => api.get<any>('/api/factory'), { interval: 15000 })
onMounted(poll.start)
const summary = computed(() => poll.data.value?.active && poll.stale.value === false ? summarizeActivity(poll.data.value, Date.now() / 1000) : null)
const items = computed(() => {
  if (!summary.value || !summary.value.fresh) return []
  const out = (Object.keys(ROLE_LABELS) as Role[]).filter(r => summary.value!.working[r]).map(r => ({ role: r, count: summary.value!.working[r], label: `${summary.value!.working[r]} ${ROLE_LABELS[r][summary.value!.working[r] === 1 ? 0 : 1]} working`, to: '/#stage-' + r }))
  if (summary.value.queued) out.push({ role: 'queued' as any, count: summary.value.queued, label: `${summary.value.queued} queued`, to: '/#columns' })
  return out
})
</script>
<template>
  <span v-if="items.length" class="inline-flex items-center gap-3 text-xs" role="status" aria-live="polite">
    <NuxtLink v-for="item in items" :key="item.role" :to="item.to" :title="item.label" :aria-label="item.label" class="inline-flex items-center gap-1 text-muted hover:text-white" :data-role="item.role">
      <span v-if="item.role !== 'queued'" class="size-3 rounded-full border border-current border-r-transparent animate-spin motion-reduce:animate-none" aria-hidden="true" />{{ item.count }}<span v-if="item.role === 'queued'">&nbsp;queued</span>
    </NuxtLink>
  </span>
</template>
```

`ui/app/components/WorkTabs.vue` (Activity `/`, Issues `/issues`; `aria-current="page"` on the active one), `ui/app/components/SettingsTabs.vue` (General `/settings`, GitHub access `/settings/github`, Coding accounts `/settings/harnesses`; `/settings/repository` highlights GitHub access), `ui/app/components/AppHeader.vue` (brand "F Forge" linking `/`, `ActivityPill`, `Settings` link, Help icon link to `/setup-help` with `aria-label="Help"`), `ui/app/layouts/default.vue`:

```vue
<template>
  <div class="min-h-screen bg-bg text-white">
    <div class="sticky top-0 z-10 border-b border-line bg-bg">
      <AppHeader />
      <WorkTabs v-if="showWorkTabs" />
      <SettingsTabs v-else-if="showSettingsTabs" />
    </div>
    <main class="mx-auto max-w-[1600px] px-4 py-6 sm:px-7">
      <slot />
    </main>
  </div>
</template>
<script setup lang="ts">
const route = useRoute()
const showWorkTabs = computed(() => ['/', '/issues'].includes(route.path) || route.path.startsWith('/issues/'))
const showSettingsTabs = computed(() => route.path.startsWith('/settings'))
</script>
```

- [ ] **Step 5: Placeholder pages so the layout renders**: `ui/app/pages/index.vue`, `issues/index.vue`, `settings/index.vue` each `<template><h1>…</h1></template>` (replaced in later tasks). Run `npm test` → PASS; `npm run build` → OK.

- [ ] **Step 6: Commit** — `git add ui && git commit -m "Add the shared layout, navigation and live activity pill"`

---

### Task 5: End-to-end harness with a mock Python API

**Files:**
- Create: `ui/tests/e2e/mock-api.mjs`, `ui/tests/e2e/run.mjs`, `ui/tests/e2e/fixtures/**` (the ready-made set copied in at Step 5, extended at Step 6), `ui/tests/e2e/smoke.spec.mjs`, `ui/README.md`, `ui/tests/unit/useForgeApi.test.ts`
- Modify: `ui/app/composables/usePoll.ts` (add the `allowed` option), `ui/app/composables/useForgeApi.ts` (add `get(path, { timeout })`), `ui/tests/unit/usePoll.test.ts` (one new case), `ui/nuxt.config.ts` (`runtimeConfig.public.pollInterval`), `.github/workflows/ci.yml` (`ui` job gets the e2e step)

**Interfaces:**
- Produces: `npm run test:e2e` which (1) builds if `.output` is missing, (2) starts `mock-api.mjs` on `127.0.0.1:8788` with fixtures and a scenario switch (`POST /__scenario {name}`), (3) starts the built app on `127.0.0.1:3999` with `NITRO_FORGE_API=http://127.0.0.1:8788`, (4) runs every `tests/e2e/*.spec.mjs` with `playwright-core` against `FORGE_BROWSER_PATH` (default `/usr/bin/chromium`), (5) exits non-zero on any failure. Specs export `default async function (page, ctx)` where `ctx = { base, scenario(name), requests }` and `requests()` resolves `GET /__requests`.
- Extends `usePoll` (Task 3) with `allowed?: () => boolean`: when it returns `false` the load is skipped and the next tick rescheduled without touching `data`, `error` or `stale`. Task 6 passes `() => !confirming`, Task 7 `() => !submitting`, Task 8 `() => !saving && !dirty`, Task 9 `() => !busy`, Task 10 the contracts §4 onboarding predicate.
- Extends `useForgeApi()` (Task 3) with `get<T>(path: string, options?: { timeout?: number }): Promise<T>` passing `AbortSignal.timeout(options.timeout ?? 15000)`. Per-screen budgets from contracts §1 "Client conventions": 15 s Activity, 30 s Issues / Settings / Repository setup, 10 s header pill.
- Fixture naming rule (used by every later task): path segments after `/api/` join with `-` — `/api/factory/output` → `factory-output.json`; a query variant appends `.<param>` — `/api/issues?issue=` → `issues.issue.json`, `/api/repositories?repo_id=` → `repositories.repo_id.json`; a POST body fixture appends `.post.json` — `/api/runtime` → `runtime.post.json`, `/api/repositories/enable` → `repositories-enable.post.json`. The `__` double-underscore spelling is retired.

- [ ] **Step 1: Add `allowed` to `usePoll` (TDD).** New case in `ui/tests/unit/usePoll.test.ts`:

```ts
it('skips the load while allowed() is false and leaves data, error and stale untouched', async () => {
  let allowed = false
  const load = vi.fn(async () => ({ n: 1 }))
  const poll = usePoll(load, { interval: 5000, allowed: () => allowed })
  poll.start(); await vi.advanceTimersByTimeAsync(0)
  expect(load).not.toHaveBeenCalled()
  expect(poll.data.value).toBe(null); expect(poll.error.value).toBe(null); expect(poll.stale.value).toBe(false)
  await vi.advanceTimersByTimeAsync(5000)
  expect(load).not.toHaveBeenCalled()
  allowed = true
  await vi.advanceTimersByTimeAsync(5000)
  expect(load).toHaveBeenCalledTimes(1)
  poll.stop()
})
```

Run → FAIL, then change `ui/app/composables/usePoll.ts`:

```ts
export interface PollOptions<T> { interval?: number; onChange?: (value: T) => void; allowed?: () => boolean }
// inside refresh(), before the request is issued:
  function refresh(): Promise<T | null> {
    if (pending) return pending
    if (options.allowed && !options.allowed()) { schedule(); return Promise.resolve(data.value) }
    pending = (async () => { /* unchanged */ })()
    return pending
  }
```

Run → PASS. (Without this a 5 s poll re-renders a half-typed reason, a dirty capacity form or an open select on Issues, Settings, Repository setup and Onboarding — every current page passes an equivalent guard.)

- [ ] **Step 2: Add the fetch timeout to `useForgeApi().get` (TDD).** New file `ui/tests/unit/useForgeApi.test.ts`:

```ts
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { useForgeApi } from '../../app/composables/useForgeApi'

const raw = vi.fn(async () => ({ ok: true, status: 200, _data: { ok: true } }))
;(globalThis as any).$fetch = { raw }
;(globalThis as any).useState = (_key: string, init: () => string) => ({ value: init() })

describe('useForgeApi().get', () => {
  beforeEach(() => raw.mockClear())
  it('defaults to a 15 s abort signal', async () => {
    const spy = vi.spyOn(AbortSignal, 'timeout')
    await useForgeApi().get('/api/factory')
    expect(spy).toHaveBeenCalledWith(15000)
    expect((raw.mock.calls[0] as any)[1].signal).toBeInstanceOf(AbortSignal)
    spy.mockRestore()
  })
  it('honours an explicit timeout', async () => {
    const spy = vi.spyOn(AbortSignal, 'timeout')
    await useForgeApi().get('/api/issues?state=open', { timeout: 30000 })
    expect(spy).toHaveBeenCalledWith(30000)
    spy.mockRestore()
  })
})
```

Run → FAIL, then change `ui/app/composables/useForgeApi.ts`:

```ts
export interface GetOptions { timeout?: number }
  async function get<T>(path: string, options: GetOptions = {}): Promise<T> {
    const response = await $fetch.raw<T & { ok?: boolean; reason?: string; controls_token?: string }>(path, {
      method: 'GET', credentials: 'same-origin', signal: AbortSignal.timeout(options.timeout ?? 15000) })
    /* unchanged body */
  }
```

Run → PASS.

- [ ] **Step 3: Make the poll interval configurable.** Add to `ui/nuxt.config.ts`:

```ts
  runtimeConfig: {
    forgeApi: 'http://127.0.0.1:8788',   // NITRO_FORGE_API overrides
    helpDoc: '../docs/onboarding.md',    // NITRO_HELP_DOC overrides
    public: { pollInterval: 5000 },      // NUXT_PUBLIC_POLL_INTERVAL overrides
  },
```

Every page reads its default interval from `useRuntimeConfig().public.pollInterval` instead of a literal `5000`, so the e2e run can drive polls fast.

- [ ] **Step 4: `mock-api.mjs`** — an `http` server:
  - GET `/api/<x>` returns `fixtures/<scenario>/<name>.json` when present, else `fixtures/default/<name>.json`, where `<name>` follows the naming rule above (`/api/factory/output` → `factory-output.json`; `?issue=` → `issues.issue.json`; `?repo_id=` → `repositories.repo_id.json`).
  - `controls_token: 'synthetic'` is injected into every object response **except** when the fixture's top level is `{"ok": false, …}`: such a fixture is served verbatim with HTTP status 200, so the `{ok:false, reason}` path of contracts §1 is exercised on every page.
  - `GET /api/repositories/ruleset` additionally sends `Content-Disposition: attachment; filename="forge-review-rules-<repo_id>.json"` so the proxy header pass-through can be asserted.
  - POST validates `X-Forge-Action === 'synthetic'`, `Origin === 'http://127.0.0.1:8788'`, `Sec-Fetch-Site === 'same-origin'`, and answers `{ok:true}` unless `fixtures/<scenario>/<name>.post.json` exists (then that file verbatim).
  - `POST /__manifest` is the GitHub App manifest stand-in: it accepts **any** content type (the browser submits `application/x-www-form-urlencoded`), skips the guard checks, records the request and replies `303` with `Location: /onboarding`.
  - Every request — GET and POST — is recorded as `{ method, path, query, body, cookie }` and exposed at `GET /__requests`; `POST /__scenario {name}` switches scenario and clears the log. Any other path → 404 `{ok:false, reason:'missing'}`.

- [ ] **Step 5: Copy the ready-made fixtures and rename per the naming rule.** A complete set already exists at `.superpowers/sdd/2026-09-22-nuxt-operator-ui/fixtures/` (7 scenarios, 15 JSON files plus a `README.md` describing each; timestamps anchored to `1790000000`, `controls_token: "synthetic"` throughout). Copy it to `ui/tests/e2e/fixtures/` and rename:

```bash
cp -R .superpowers/sdd/2026-09-22-nuxt-operator-ui/fixtures/. ui/tests/e2e/fixtures/
cd ui/tests/e2e/fixtures
mv default/factory__output.json        default/factory-output.json
mv default/issues__issue.json          default/issues.issue.json
mv default/repositories__repo_id.json  default/repositories.repo_id.json
mv needs-setup/repositories__repo_id.json needs-setup/repositories.repo_id.json
mv empty no-repos     # E8's "no repositories" scenario: issues [], repositories [], complete true
```

Keep the copied `README.md` next to the fixtures and add the new scenarios below to it.

- [ ] **Step 6: Add the missing scenarios.** Write these files under `ui/tests/e2e/fixtures/`:
  - `paused/factory.json`, `paused/issues.json`, `paused/settings.json` — each exactly `{"ok": false, "reason": "Factory reset is in progress"}` (the reset fence of contracts §1 GET guard 2). Tasks 6–8 each assert their page's behaviour in this scenario.
  - `stop-rejected/factory.json` — `default/factory.json` plus `actions: [{ "id": "r1", "action": "stop", "target": "<the running build job id>", "reason": "Stuck", "status": "rejected", "error": "Worker already stopped" }]`.
  - `moved/factory.json` — `default/factory.json` with issue `1:601` at `state: "built"` (the same issue one column further right), for the column-move animation and the aria-live announcement.
  - `decision-pending/issues.issue.json` — `default/issues.issue.json` plus `actions: [{ "id": "a1", "key": "1:613", "status": "pending", "message": null }]`.
  - `setup/factory.json` — `{"ok": false, "reason": "Factory state is unavailable; reconcile the durable ledger."}` (the realistic pre-activation response; `/` runs both the redirect middleware and the header pill against `/api/factory`).
  - `setup/onboarding.post.json` — `{"ok": true, "action": "http://127.0.0.1:8788/__manifest", "manifest": {"name": "forge-build"}}`.
  - `default/runtime.post.json` — `default/factory.json` with `ok: true` and `runtime.slots.build = 3` (POST `/api/runtime` responds `FactoryView & {ok:true}`; a bare `{ok:true}` would blank the page's `runtime`).
  - `default/repositories-enable.post.json` — `default/repositories.json` with `ok: true`, the target row `enabled: true` and `protection_status: "ready"`.
  - `help.md` — a fixed help document with exactly seven `#`/`##` headings, including `## Host installation` and `## GitHub repository protection`, used instead of the live `docs/onboarding.md` (Task 11).

  Also extend `default/factory.json` so Task 6's fixed-height assertion has something to bite on. It must contain: one issue whose title is longer than 120 characters; one queued issue with no job and no reason; one issue with `prerequisite_hold: true`; one issue with `failure_count: 2`; one running job carrying both `provider` and `model`; and one recovery job whose `issue_key` (`3:900`) has **no** `LedgerIssueView` entry in `issues[]`.

- [ ] **Step 7: `run.mjs`** — spawns mock + app (waits for both ports), launches `chromium.launch({ headless: true, chromiumSandbox: true, executablePath })`, iterates specs, prints `PASS <name>`/`FAIL <name>` with the error, closes everything, `process.exit(failures ? 1 : 0)`. The app is started with `NITRO_FORGE_API=http://127.0.0.1:8788`, `NUXT_PUBLIC_POLL_INTERVAL=300` (so a poll-driven assertion takes ~300 ms instead of 5 s) and `NITRO_HELP_DOC=tests/e2e/fixtures/help.md` (so the help specs never depend on the live `docs/onboarding.md`).

- [ ] **Step 8: `smoke.spec.mjs`**

```js
import assert from 'node:assert/strict'
export default async function (page, { base }) {
  await page.goto(base + '/')
  assert.equal(await page.title(), 'Activity · Forge')
  await page.getByRole('link', { name: 'Issues' }).click()
  await page.waitForURL(base + '/issues')
  const status = await page.evaluate(async () => (await fetch('/api/factory')).status)
  assert.equal(status, 200)
}
```

- [ ] **Step 9: Run inside the runner image** (documented in `ui/README.md`):

```bash
cd ~/forger-remote/ui && npm run build
docker run --rm --network none --security-opt seccomp=unconfined --tmpfs /private/tmp -v ~/forger-remote:/src:ro -w /src/ui \
  -e HOME=/tmp -e FORGE_BROWSER_PATH=/usr/bin/chromium -e FORGE_PLAYWRIGHT_MODULE=/usr/local/lib/node_modules/playwright-core \
  forge-runner:local node tests/e2e/run.mjs
```
Expected: `PASS smoke`. (The `.output` bundle is self-contained; `node_modules` are not needed inside the image except `playwright-core`, which the image provides.)

- [ ] **Step 10: CI** — add to the `ui` job: `- run: npx playwright-core install chromium --with-deps` and `- run: npm run test:e2e` with `FORGE_BROWSER_PATH` unset (run.mjs falls back to playwright's bundled chromium when the env var is empty).

- [ ] **Step 11: Commit** — `git add ui .github/workflows/ci.yml && git commit -m "Add the end-to-end harness with a mock Forge API"`

---

### Task 6: Activity page (Factory) with fixed-size cards and single banners

**Files:**
- Create: `ui/app/utils/factory.ts`, `ui/app/components/IssueCard.vue`, `ui/app/components/WarningBanner.vue`, `ui/app/components/StageColumn.vue`, `ui/app/components/ConfirmDialog.vue`, `ui/app/components/MetricTile.vue`, `ui/app/pages/index.vue`
- Test: `ui/tests/unit/factory.test.ts`, `ui/tests/e2e/factory.spec.mjs`, fixtures `default/factory.json`, `warning/factory.json`, `stop-rejected/factory.json`, `moved/factory.json`, `paused/factory.json` (all created in Task 5; `warning/factory.json` carries `observation.errors = [{issue:'1:824', phase:'github', reason:'GitHub observation or publication is unconfirmed'}]` and `operator = {title:'Forge needs attention', detail:'GitHub observation or publication is unconfirmed', action_url:'/settings', action_label:'Review setup'}`).

**Interfaces:**
- Consumes: contracts §2 `FactoryView`, `JobView`, `LedgerIssueView`; §3.1 helper rules; `usePoll` (with `allowed`), `useForgeApi` (`get` with a 15 s timeout), `formatDuration`, `clampTitle`.
- Produces in `ui/app/utils/factory.ts`:
  - `maxAge(runtime)`, `isFresh(view, now)`, `jobFor(view, issue)`, `isRunning(view, job, now)`
  - `phaseLabel(issue, job, view, now): string` (table in contracts §3.1 "phase")
  - `reasonText(issue, job): string` (contracts §3.1 "reason"), **returning `''` when a job exists and the reason is `blocked_auth` or `blocked_rate_limit`** — otherwise every running card grows a stale sign-in sentence.
  - The flat card model every card-shaped surface renders, so no component reaches into `FactoryView`:

```ts
export interface CardModel {
  key: string                 // "<repo_id>:<number>"
  id: string                  // "issue-<repo_id>-<number>" — anchor target and FLIP key
  to: string                  // "/issues/<encodeURIComponent(key)>" (internal jobs append "?job=<jobId>")
  repo: string                // "XRPL-Commons/academy"
  number: number | null       // null for an internal job with no issue
  title: string               // already clampTitle()-d
  phaseLabel: string
  providerLine: string        // "<provider> · <model>", '' when unknown
  reason: string              // reasonText(issue, job)
  timerStart: number | null
  timerEnd: number | null
  running: boolean
  stoppable: boolean          // job && job.role !== 'merge' && !job.cleanup && !job.outcome
  jobId: string | null
  footerLink: { to: string; label: string } | null
}
```

  - `groupIssues(view, now): { columns: Record<'build'|'review'|'audit', CardModel[]>; attention: CardModel[]; finishing: CardModel[]; investigating: CardModel[]; internalJobs: CardModel[]; completed: CardModel[] }` (assignment order columns → attention → finishing → runners → completed; an issue appears once).
  - `toCard(job: JobView, view: FactoryView, now: number): CardModel` — the adapter for an internal job that has no `LedgerIssueView`: `number: null`, `title` from `job.issue_key`, `to: '/issues/' + encodeURIComponent(job.issue_key) + '?job=' + job.id`. `groupIssues` uses it for `internalJobs`.
  - `footerLink` rule (fixed-height footer row 2, at most one entry): `{ to: '/settings/repository?repo_id=<repo_id>', label: 'Prepare repository →' }` when `prerequisite_hold`; else `{ to: '/settings/harnesses', label: 'Coding accounts →' }` when the reason is `blocked_auth` and there is no job; else `{ to: '', label: '<failure_count> / 3 attempts failed' }` when `failure_count > 0`; else `null`. (The design rule "links to **GitHub** live in the detail view only" removes GitHub links, not internal remediation links.)
  - `metrics(view, now): Tile[]` — seven tiles, each with a detail line and a destination:

```ts
export interface Tile {
  id: 'queued' | 'build' | 'review' | 'audit' | 'recovery' | 'attention' | 'merged'
  label: string; value: number; detail: string; to: string; tone: 'ok' | 'warn' | 'muted'
}
```

    Queued — detail `'Waiting for a worker'`, or `'Dispatch paused'` with tone `muted` when `!runtime.enabled`; `to: '#columns'`.
    Builders / Reviewers / Auditors — detail `'<slots[role]> maximum'` when the value is 0, else `'<value> of <slots[role]> running'`; `to: '#stage-<role>'`.
    Investigations — recovery count, detail `'<slots.recovery> maximum'`, `to: '#stage-recovery'`.
    Needs attention — detail `'Awaiting a decision'`, tone `warn` when the value is > 0, `to: '#needs-attention'`.
    Merged — detail `'All tracked issues'`, `to: '#completed'`.
    Recovery is counted as working (see Global Constraints).
  - `warnings({ factory, onboarding }, now, currentPath): Warning[]` — the single producer of banner objects, reused verbatim by Task 8. No component constructs a warning object inline.

```ts
export type WarningKind = 'github' | 'capacity' | 'runner' | 'reconciliation' | 'other'
export interface Warning { kind: WarningKind; text: string; to: string; label: string }

// [primary, fallback-when-primary-is-the-current-route]
const WARNING_DESTINATIONS: Record<WarningKind, [[string, string], [string, string]]> = {
  github:         [['/settings/github', 'Open GitHub access'], ['/setup-help#github-repository-protection', 'Open help']],
  capacity:       [['/settings', 'Open worker capacity'], ['/settings#worker-capacity', 'Open worker capacity']],
  runner:         [['/setup-help#host-installation', 'Check service setup'], ['/setup-help', 'Open help']],
  reconciliation: [['/setup-help#configuration-and-recovery', 'Open help'], ['/setup-help', 'Open help']],
  other:          [['/setup-help', 'Open help'], ['', '']],
}

export function destination(kind: WarningKind, currentPath: string): { to: string; label: string } {
  const [primary, fallback] = WARNING_DESTINATIONS[kind]
  const [to, label] = primary[0] === currentPath ? fallback : primary
  return { to, label }   // to === '' → WarningBanner renders no button
}

export function warnings(
  view: { factory: FactoryView | null; onboarding?: OnboardingView | null },
  now: number,
  currentPath: string,
): Warning[] {
  const out: Warning[] = []
  const seen = new Set<string>()
  for (const error of view.factory?.observation?.errors ?? []) {
    if (seen.has(error.reason)) continue
    seen.add(error.reason)
    const kind: WarningKind = (['github', 'capacity', 'runner', 'reconciliation'] as const)
      .includes(error.phase as any) ? (error.phase as WarningKind) : 'other'
    out.push({ kind, text: error.reason, ...destination(kind, currentPath) })
  }
  for (const action of view.factory?.actions ?? []) {
    if (action.status !== 'rejected' || !action.error) continue
    const text = 'Action could not be applied: ' + action.error
    if (seen.has(text)) continue
    seen.add(text)
    out.push({ kind: 'other', text, ...destination('other', currentPath) })
  }
  if (view.onboarding?.error && (view.onboarding.checks?.provider === 'failed' || view.onboarding.checks?.runner === 'failed'))
    out.push({ kind: 'runner', text: view.onboarding.error, to: '/settings/harnesses', label: 'Open coding accounts' })
  return out
}
```

    `observation.errors[].phase` is `'runner' | 'reconciliation' | 'github' | 'capacity' | string` (contracts §2). There is no `provider` phase; do not add one. A warning never emits a button pointing at the route it is displayed on.
  - `elapsed(seconds): string` per contracts ("N sec", "N min", "Nh Mm").
- `IssueCard` props: `{ card: CardModel }`; emits `stop(jobId: string)`; root element `:id="card.id"` and `:data-key="card.key"` with class `issue-card`; fixed height `h-[168px] sm:h-[168px]` (`h-[136px]` below `sm`), title `line-clamp-2`, reason `truncate`; footer row 1: `UButton` "Open" → `card.to`; `UButton` "Stop" (variant ghost, color error) when `card.stoppable`; footer row 2: `card.footerLink` (fixed height, empty when `null`). No GitHub links, no Output button. The card renders a fixed set of slots, never a conditional tree, so every card is the same height.
- `MetricTile` props `{ label: string; value: number | string; detail?: string; to?: string; tone?: 'ok'|'warn'|'muted' }`; when `to` is set the whole tile is a `NuxtLink`.
- `WarningBanner` props `{ warning: Warning }` → `UAlert` with `color="warning"`, `role="status"`, one action button linking `warning.to` labelled `warning.label`, and **no** button when `warning.to === ''`.
- `ConfirmDialog` props `{ open, title, text, confirmLabel, reasonLabel = 'Decision or reason', requireReason = true }`, emits `confirm(reason)` / `close`; built on `UModal` + `UTextarea`. It is the only confirmation UI in the app (Task 7's `DecisionPanel` wraps it).

- [ ] **Step 1: Failing unit tests `ui/tests/unit/factory.test.ts`** covering: `phaseLabel` for a running job → "Working", stale running → "Last seen working", `capacity_wait_since` → "Waiting for memory", no job + state `approval_needed` → "Approval needed"; `reasonText` for `blocked_auth` with a Claude availability event and **no job** → "Claude Code needs sign-in. Queued for another available coding account.", and the same issue **with** a job → `''`; `groupIssues` puts a `ready` issue with no job in `columns.build`, a `failed` issue in `attention`, an `audited` issue in `finishing`, a recovery job with no issue in `internalJobs` (via `toCard`, `to` ending `?job=<id>`), sets `id` to `issue-1-601` and never lists an issue twice; `groupIssues` emits `footerLink.label === 'Prepare repository →'` for a `prerequisite_hold` issue and `'2 / 3 attempts failed'` for `failure_count: 2`; `metrics` returns seven tiles, counts a fresh running recovery job in the `recovery` tile and a stale one nowhere, gives the Queued tile detail "Dispatch paused" when `runtime.enabled` is false, and gives the `build` tile `to === '#stage-build'`; `warnings` dedupes identical reasons, maps `phase:'github'` to `/settings/github` "Open GitHub access", `phase:'capacity'` to `/settings` "Open worker capacity", `phase:'runner'` to `/setup-help#host-installation` "Check service setup", `phase:'reconciliation'` to `/setup-help#configuration-and-recovery` "Open help", an unknown phase to `/setup-help` "Open help", and — called with `currentPath === '/settings'` — returns `/settings#worker-capacity` for the capacity warning.

Build the fixture inline from contracts §2 (`observation.at = now - 10`, `runtime.cycle_seconds = 30`, `slots = {build:2, review:1, audit:1, recovery:1}`).

- [ ] **Step 2: Run** `npx vitest run tests/unit/factory.test.ts` → FAIL.

- [ ] **Step 3: Implement `ui/app/utils/factory.ts`** exactly per the rules quoted in contracts §3.1 (copy the `nextRole`, `phase()`, `reason()`, `metrics()`, `groups` logic; the deliberate changes are counting recovery jobs in the Investigations tile and in `warnings()`, returning `CardModel`s instead of raw payload rows, and the `currentPath` argument of `warnings()`).

- [ ] **Step 4: Run** → PASS.

- [ ] **Step 5: Components and page.** `pages/index.vue`:
  - `useHead({ title: 'Activity' })`, which the `titleTemplate: '%s · Forge'` of Task 1 renders as "Activity · Forge". The contracts' "Forge · Factory" title is deliberately replaced by the new tab names (Activity / Issues / Settings / GitHub access / Prepare repository / Coding accounts / Setup / Reset Forge / Setup help).
  - `usePoll(() => api.get('/api/factory', { timeout: 15000 }), { allowed: () => !confirming.value })` started on mount, interval from `useRuntimeConfig().public.pollInterval`, plus a 1 s `now` ticker paused when hidden. `confirming` is true while a `ConfirmDialog` is open or a POST is in flight, so a poll never re-renders under the operator's hands.
  - Header block: eyebrow `Live operations` rendered with `uppercase tracking-wide` styling (the string itself is sentence case — the contracts' "LIVE OPERATIONS" is CSS, not text), h1 "Factory", freshness text per contracts §3.1 ("Connecting…", "Live · checked <elapsed> ago", "Updates delayed · last known state", "Reconnecting · last known state").
  - **At most one message block is rendered above the metrics**: connection lost → the freshness pill only; otherwise the banner(s); otherwise the operator status line.
  - Operator status line: `operator.title` and `operator.detail` once, `operator.action_label` as a single `UButton` link. When a warning's `text` equals `operator.detail`, the status line is suppressed and the banner absorbs it: `UAlert` title = `operator.title`, description = `operator.detail`, one button = `operator.action_label` → `operator.action_url`, falling back to `destination(kind, route.path)` when `operator.action_url` equals the current route. The page never renders both.
  - Deliberately dropped: the contracts §3.1 notice sentence "Connection interrupted. Showing the last known state; retrying automatically." — it is superseded by the freshness pill "Reconnecting · last known state", because only one message block may appear above the metrics. Do not re-add it.
  - `WarningBanner` per `warnings({ factory: poll.data.value }, now, route.path)`; the amber duplicate line is gone.
  - Seven `MetricTile`s from `metrics()`: Queued, Builders, Reviewers, Auditors, Investigations (recovery count), Needs attention, Merged, in a responsive grid; each tile is a link to its `to` so a count always leads to the cards behind it.
  - Three `StageColumn`s (Build/Review/Security audit) inside a `<section id="columns">`, each `<div id="stage-build|stage-review|stage-audit">` and a list of `IssueCard`; summary line per contracts; empty "No issues at this stage.".
  - Panels: `<section id="needs-attention">` "Needs attention · n"; "Finishing" with the note "Approved work waiting to merge, or being merged." and empty state "No issues waiting to merge."; a collapsible `<section id="runners">` containing `<div id="stage-recovery">` titled "Investigations and cleanup · n" with the note "Internal recovery is shown separately from productive workers.", rendered **expanded** whenever it holds a job with `phase === 'running'`; `<section id="completed">` collapsible "Completed issues" with empty state "No completed issues yet.". The header pill's merge link resolves to `#runners` and its recovery link to `#stage-recovery`, so every anchor `ActivityPill` builds exists.
  - Each Investigations and cleanup row is an `IssueCard` fed by `toCard(job)`: "Open" → `/issues/<job.issue_key>?job=<job.id>` (a `JobView` always carries `issue_key`) plus a "Reset" button → ConfirmDialog "Reset runner?". Recovery investigations, orphaned cleanup jobs and failed attempts therefore keep a route to their logs (Task 7 renders `?job=`).
  - Stop → ConfirmDialog "Stop this attempt?" with the contracts text and confirm "Stop attempt"; submit `api.act('/api/factory/action', { action: 'stop', target: card.jobId, reason })`; success toast "Action queued for the supervisor." via `useToast()`; error shown inside the dialog. A rejected action comes back through `warnings()` as "Action could not be applied: <error>".
  - Card moves between columns: `<TransitionGroup name="card">` keyed on `card.key` with a 420 ms transform, disabled under `prefers-reduced-motion`; announce moves in a `sr-only` `aria-live` region "Issue <n> moved to <column>".
  - `{ok:false, reason}` payloads (scenario `paused`) render as the single banner "Factory reset is in progress" with no metrics and no columns.

- [ ] **Step 6: e2e `ui/tests/e2e/factory.spec.mjs`**: loads `/`; every `.issue-card` has the same `boundingBox().height` (the `default` fixture deliberately mixes a >120-character title, a queued issue with no job and no reason, a `prerequisite_hold` issue, a `failure_count: 2` issue, a running job with provider+model and a recovery job with no issue row); a long title is clamped (element `scrollHeight` ≤ `clientHeight`); no button named "Output"; card "Open" navigates to `/issues/<key>`; an Investigations row "Open" navigates to `/issues/<issue_key>?job=<id>`; with scenario `warning`: the text "GitHub observation or publication is unconfirmed" appears exactly once, one button "Open GitHub access" with href `/settings/github`, and no heading "Forge needs attention" repeated below the status; Stop opens the dialog, requires a reason, posts `{action:'stop', target, reason}` (check `/__requests`), shows the toast; then `ctx.scenario('stop-rejected')` and, after one poll, the banner "Action could not be applied: Worker already stopped" is visible; `ctx.scenario('moved')` and, after one poll, the card for issue 601 sits in the Review column and the `sr-only` region reads "Issue 601 moved to Review"; `ctx.scenario('paused')` shows "Factory reset is in progress" and no `.issue-card`; viewport 390 px: cards fill the width, no horizontal scroll.

- [ ] **Step 7: Run e2e in the runner image** (Task 5 command) → PASS. **Commit**: `git add ui && git commit -m "Rebuild the Activity page with fixed cards and single warning banners"`

---

### Task 7: Issues list and issue detail route with worker output

**Files:**
- Create: `ui/app/utils/issues.ts`, `ui/app/components/IssueRow.vue`, `ui/app/components/IssueDetail.vue`, `ui/app/components/DecisionPanel.vue`, `ui/app/components/MarkdownBody.vue`, `ui/app/pages/issues/index.vue`, `ui/app/pages/issues/[key].vue`, `ui/server/utils/markdown.ts`, `ui/server/routes/_ui/markdown.post.ts`
- Test: `ui/tests/unit/issues.test.ts`, `ui/tests/unit/markdown.test.ts`, `ui/tests/e2e/issues.spec.mjs`, fixtures `default/issues.json` (list; rows from `tests/browser_issues.cjs`), `default/issues.issue.json` (detail; mock-api maps the query `issue=<key>` to `issues.issue.json`), `default/factory-output.json` (`{ok:true, logs:'line one\nline <b>two</b>'}`), `decision-pending/issues.issue.json`, `no-repos/issues.json`, `paused/issues.json` — all created in Task 5.

**Interfaces:**
- Consumes: contracts §2 `IssueListResponse`, `IssueDetailResponse`, §3.2–3.3 copy tables; `usePoll` (with `allowed`), `useForgeApi` (`get` with a 30 s timeout), `ConfirmDialog` (Task 6).
- Produces in `ui/app/utils/issues.ts`: `STATUS_NAMES`, `ACTION_LABELS`, `ACTION_EFFECTS`, `EVENT_LABELS`, `SHOW_OPTIONS` (value/label pairs for the Show select), `rowAction(row, pending): string`, `listParams({ state, repoId, q, page }): string`, `freshnessText(list): string`, `emptyState(list): { title: string; text: string; link?: { to: string; label: string } } | null`, `decisionNeedsReason(action): boolean`.
- Nitro auto-maps only `server/api/**` and `server/routes/**`, so the markdown endpoint lives at `ui/server/routes/_ui/markdown.post.ts` and is called as `POST /_ui/markdown` — a prefix that is also clear of `proxyPaths()`, which matches `/api` and `/api/*`. Body `{ text: string; repo: string }` → `{ html: string }` rendered with `markdown-it` (GFM tables, no raw HTML) and `sanitize-html` restricted to the tag/attribute list in contracts §3.3, links resolved against `https://github.com/<repo>/blob/HEAD/`, only http/https/mailto kept, `target="_blank" rel="noopener noreferrer"`, non-checkbox inputs removed, checkboxes disabled. Text limited to 16000 chars; body limited to 64 KiB.
- `pages/issues/[key].vue` renders `IssueDetail` full-page — the route is the one shared link. `pages/issues/index.vue` renders the list only; a row click is a route navigation to `/issues/<key>`. There is no slideover and no second detail view.
- `pages/issues/index.vue` redirects `?issue=<key>` to `/issues/<encodeURIComponent(key)>` during setup (server-side `navigateTo('/issues/' + encodeURIComponent(key), { redirectCode: 302 })`), because the API hands the UI `/issues?issue=<key>` in `operator.action_url` (contracts §2, three rows) and in `setup_url`-style deep links. One URL shape is ever rendered.
- `IssueDetail` props `{ issueKey: string }` (`key` is reserved by Vue and cannot be declared as a prop).
- `MarkdownBody` props `{ html: string }` — a presentational component that never fetches.
- `DecisionPanel` props `{ open, action, detail }` renders `ConfirmDialog` with `title`/`text`/`confirmLabel`/`requireReason` derived from `ACTION_LABELS` / `ACTION_EFFECTS` / `decisionNeedsReason(action)` and emits `submit({ action, reason })`. It knows no endpoints; the page owns both POSTs.

- [ ] **Step 1: Failing tests.** `issues.test.ts`: `listParams({state:'open', repoId:2, q:' logging ', page:3})` → `state=open&page=3&repo_id=2&q=logging`; `rowAction` for pending → "Applying…", working → "View progress", attention → "Review", else "View issue"; `freshnessText` for a scope error returns that error, for `complete` returns "Up to date · Viewing issues never queues them for work.", and for an incomplete inventory returns "Showing received issues. The GitHub inventory is still incomplete or needs refreshing."; `emptyState` for no repositories returns the "Choose where Forge should work" object with link `/settings/repository`; `decisionNeedsReason` true for pause/resume/retry/stop, false for queue/backlog/recheck. `markdown.test.ts` (imports the pure `renderMarkdown(text, repo)` from `ui/server/utils/markdown.ts`): strips `<script>`, keeps a table, rewrites `[a](docs/x.md)` to the GitHub blob URL with `target="_blank"`, drops `javascript:` links to plain text, disables checkboxes.

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** `ui/app/utils/issues.ts` (copy the maps verbatim from contracts §3.2–3.3), `ui/server/utils/markdown.ts` + `ui/server/routes/_ui/markdown.post.ts` (`npm install markdown-it sanitize-html` and their `@types`). **Step 4: Run** → PASS.

- [ ] **Step 5: Components and pages.**
  - List page: `useHead({ title: 'Issues' })` → "Issues · Forge" (the contracts' "Forge · Issues" is deliberately replaced by the new tab names); eyebrow "Your work", h1 "Issues", intro per contracts; filters: `UInput` search (debounced 200 ms, resets page), `USelectMenu` "Repository" (items "All repositories" + scopes), `USelectMenu` "Show" (`SHOW_OPTIONS`); refresh `UButton` icon `aria-label="Refresh issues"` → `api.act('/api/issues/refresh', {})` then `refresh()`. Poll: `usePoll(() => api.get('/api/issues?' + listParams(...), { timeout: 30000 }), { allowed: () => !submitting.value })` re-created when params change (watch params → `refresh()`). Rows via `IssueRow`; pagination "Previous"/"Next" + count text; empty states from `emptyState()`.
  - The freshness line renders directly under the filters on every load, from `freshnessText(list)` plus `usePoll`'s `stale`/`error`: "Up to date · Viewing issues never queues them for work." when `complete`; the scope error verbatim when one is present; "Showing received issues. The GitHub inventory is still incomplete or needs refreshing." when the inventory is incomplete.
  - Row-level "Applying…" pill: `pending` handed to `rowAction(row, pending)` is the last `actions[]` entry for the row key with `status === 'pending'`.
  - `IssueDetail` props `{ issueKey }`: polls `/api/issues?issue=<issueKey>` at the configured interval with a 30 s timeout and `allowed: () => !submitting.value`; renders meta, title, status `UBadge`, explanation (+ environment blocker), reviews lines, `MarkdownBody :html="html"`, links "Open on GitHub ↗" and "Pull request #n ↗", "Depends on #a, #b" links to `/issues/<repo_id>:<n>`, action buttons from `detail.actions` with `ACTION_LABELS` (primary for queue/resume/retry/recheck), "Repository setup →" when `setup_needed`, "Stop attempt" when `active && status === 'working'`, and the "Recent activity" list with `EVENT_LABELS`.
  - Markdown is rendered server-side on the first paint: `const { data: html } = await useAsyncData('md-' + issueKey, () => $fetch('/_ui/markdown', { method: 'POST', body: { text: detail.body, repo: detail.repo } }).then(r => r.html), { watch: [() => detail.body] })`. `MarkdownBody` shows "No description has been added yet." when the body is empty and the note "Description shortened. Open on GitHub to read the full issue." when `body_truncated`.
  - **Worker output** `UCollapsible`: it reads `?job=<id>` from the route when present, else `issue.active`; below it the detail lists the issue's jobs (role, phase, outcome, started_at) newest first and selecting one sets `?job=`. It loads `/api/factory/output?id=<selected>` on first expand into a `<pre class="whitespace-pre-wrap">` with escaped text, or "No captured output is available yet.". Logs therefore stay reachable for a recovery investigation, an orphaned cleanup job and a finished failed attempt — not only while a job is attached and running.
  - Detail error copy: "This issue is not in a managed repository. Refresh the issue list." when the detail 404s or the repo is unmanaged; "Showing the last received issue. Refresh before making a decision." as the stale-detail message when `usePoll.stale` is true.
  - Open `UCollapsible` state (Worker output, Recent activity) and textarea contents live in component state keyed by issue key, so a poll can never collapse a panel or clear a half-typed reason. (The in-place DOM patching of `ForgeRefresh.html` — focus, selection and open-`<details>` preservation — is otherwise deliberately deferred: Vue keying covers the cases that matter.)
  - `DecisionPanel` emits `submit({ action, reason })`; the page posts stop → `/api/factory/action {action:'stop', target: detail.active, reason}` and everything else → `/api/issues/action {key, action, revision, reason}`. When `decisionNeedsReason(action)` is false the reason defaults to `<label> confirmed by operator`. Success message "Decision submitted. Waiting for confirmation."; afterwards the message follows `actions[]` for the key ("Applying your decision…" / its `message`).

- [ ] **Step 6: e2e `issues.spec.mjs`**: filters use the select menus (`getByRole('button', {name: /Show/})`, pick "Completed", the recorded GET has `state=completed` — the mock records every request as `{method, path, query, body, cookie}`); search debounce produces one request with `q=`; row click opens `/issues/1:613` and shows the title; `/issues?issue=1:613` lands on `/issues/1%3A613`; "Worker output" is present for the working row, loads on expand and renders the escaped `<b>` literally; opening `/issues/1:604?job=<recovery job id>` shows that job's output without the issue being active; Queue decision posts `{key, action:'queue', revision, reason:'Queue for Forge confirmed by operator'}`; Pause requires a reason; Stop posts to `/api/factory/action`; refresh button posts `/api/issues/refresh`; with `ctx.scenario('decision-pending')` the row and detail message read "Applying your decision…"; `ctx.scenario('no-repos')` shows "Choose where Forge should work"; `ctx.scenario('paused')` shows "Factory reset is in progress"; 390 px layout has no horizontal scroll.

- [ ] **Step 7: Run unit + e2e** → PASS. **Commit**: `git commit -m "Rebuild Issues with a detail route that exposes worker output"`

---

### Task 8: Settings · General as the factory status page

**Files:**
- Create: `ui/app/utils/status.ts`, `ui/app/components/StatusRow.vue`, `ui/app/components/CapacityForm.vue`, `ui/app/pages/settings/index.vue`, `ui/app/pages/settings/advanced.vue` (re-exports General)
- Test: `ui/tests/unit/status.test.ts`, `ui/tests/e2e/settings-general.spec.mjs`, fixtures `default/settings.json`, `default/onboarding.json`, `default/factory.json`, `default/runtime.post.json`, `provider-failed/onboarding.json` (`checks.provider='failed'`, harness claude model status `'failed'`, `error: 'The claude native tool sandbox failed verification inside the runner. Check the runner image and the host isolation policies'`), `paused/settings.json` — all created in Task 5.

**Interfaces:**
- Consumes: `FactoryView` (runtime, runtime_revision, observation, operator), `SettingsView` (roles, sync), `OnboardingView` (harnesses, checks, error, phase, service, qualification_fresh); `warnings()` and `WarningBanner` from Task 6; `summarizeActivity()` from Task 4; `usePoll` (with `allowed`), `useForgeApi` (`get` with a 30 s timeout).
- Produces `statusRows({ factory, settings, onboarding }, now): StatusRow[]`:

```ts
export interface StatusRow { id: 'dispatch'|'image'|'accounts'|'github'|'observation'; label: string; value: string
  tone: 'ok'|'warn'|'muted'; detail?: string; to?: string; linkLabel?: string }

export function statusRows(
  sources: { factory: FactoryView | null; settings: SettingsView | null; onboarding: OnboardingView | null },
  now: number,
): StatusRow[]
```

  The three payloads arrive from one `Promise.all` but can each fail independently, so **a row whose source is missing renders `value: 'Not available'`, `tone: 'muted'` and no link** — missing data never produces a warn tone.
  - dispatch (from `factory`): "Enabled" / "Paused" (`runtime.enabled`), detail `operator.detail` when paused, link `/settings` "Worker capacity".
  - image (from `factory` + `onboarding`): `runtime.image` truncated at the tag boundary (everything up to and including `:<tag>`, never a fixed 19-character slice) + " · verification current" when `onboarding.qualification_fresh`, else + " · verification required" with tone `warn`; link `/settings/harnesses`. `OnboardingView` carries no verification timestamp, so no "verified <age>" can be rendered.
  - accounts (from `onboarding`): one sentence built from `harnesses[]`, never a hardcoded list of names — all passed → "All coding accounts verified."; otherwise `"<name> <status>"` entries joined with " · " using the **raw** status text from `Harness.models[].status` (`connected|passed|failed|unknown|'sign in again'|'usage limit; retry pending'|'verification required'`); tone `warn` when any status is `failed` or `verification required`; tone `muted` when the only non-passed status is `usage limit; retry pending` (that one is self-healing and must not read like an operator task); link `/settings/harnesses` "Coding accounts". `detail` is omitted — `onboarding.error` is rendered once, by the banner.
  - github (from `settings`): count of roles with status `ok` — "4 of 4 Apps verified" / "n of 4", tone warn when < 4, link `/settings/github`.
  - observation (from `factory`): "Supervisor seen <age> ago" / "No observation yet", tone warn when older than `maxAge`.
- `CapacityForm` props `{ runtime, revision, working: Record<role, number>, locked: boolean, lockedReason: string }` → inputs Builders/Reviewers/Security auditors with a live "· n working" suffix; emits `saved(view)` / `discarded`; posts `/api/runtime {config, revision}` and re-renders its inputs from the returned `FactoryView`. When `locked` is true the inputs and Save are disabled and `lockedReason` is displayed: POST guard 8 of the contracts makes `/api/settings` and `/api/runtime` answer `{ok:false, reason:"Use guided setup until the factory has started"}` while `setup.json.phase != 'active'`, so the page passes `locked = onboarding?.phase !== 'active'` and that exact sentence as `lockedReason`.

- [ ] **Step 1: Failing test `status.test.ts`**: paused runtime → dispatch row "Paused" tone warn; all harness models passed → accounts row "All coding accounts verified." tone ok; failed provider → accounts row "Claude Code failed" tone warn with no `detail`; a lone "usage limit; retry pending" → tone muted; all roles ok → "4 of 4 Apps verified" tone ok; stale observation (older than maxAge) → tone warn; `qualification_fresh: false` → image row ends " · verification required" tone warn and the value keeps the whole `repo:tag`; `statusRows({ factory: null, settings: null, onboarding: null }, now)` → every row `value: 'Not available'`, `tone: 'muted'`, `to` undefined.

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement `status.ts`**. **Step 4: Run** → PASS.

- [ ] **Step 5: Page.** `settings/index.vue`:
  - `useHead({ title: 'Settings' })` → "Settings · Forge" (the contracts' "Forge · Settings" is deliberately replaced by the new tab names); eyebrow "Settings", h1 "General", intro "Manage worker capacity and factory setup.".
  - One poll of all three payloads together — `usePoll(() => Promise.all([api.get('/api/settings', { timeout: 30000 }), api.get('/api/onboarding', { timeout: 30000 }), api.get('/api/factory', { timeout: 30000 })]), { allowed: () => !saving.value && !dirty.value })` — at the configured interval. The factory payload is **not** fetched once: the observation row ("Supervisor seen <age> ago", tone warn past `maxAge`) and `CapacityForm`'s live "· n working" both age, and against a frozen payload they decay into a false amber. Worker counts come from `summarizeActivity(factory, now)`.
  - Top: "Status" card with the five `StatusRow`s.
  - Then `WarningBanner`s from the single producer `warnings({ factory, onboarding }, now, route.path)` (the same function Task 6 uses) — which already emits the `onboarding.error` banner linking `/settings/harnesses` when `checks.provider === 'failed'` or `checks.runner === 'failed'`. No page builds a banner of its own.
  - Freshness surface: when `usePoll.stale` is true, render "Could not refresh. Showing the last received data. <message>" above the Status card, `<message>` being `usePoll.error`.
  - Then a "Worker capacity" card (`id="worker-capacity"`, the anchor `warnings()` falls back to when a capacity warning is shown on `/settings`) with `CapacityForm` (copy from contracts §3.4, keep the note text and the save/discard messages).
  - "Continue setup" link when `onboarding.phase !== 'active'`; bottom "Reset Forge" danger card verbatim from contracts §3.4 with link "Review reset…" → `/reset`.

- [ ] **Step 6: e2e**: General shows the five status rows; provider-failed scenario shows the failure sentence exactly once (banner only, not also in the accounts row) and a banner button "Open coding accounts"; changing Builders to 3 and saving posts `/api/runtime` with `config.slots.build === 3` and the inputs re-render from `runtime.post.json` (Builders reads 3 after the response, proving the full `FactoryView` echo is consumed); Discard restores; with an `onboarding.phase` that is not `active` the inputs are disabled and "Use guided setup until the factory has started" is shown; `ctx.scenario('paused')` shows "Factory reset is in progress"; mobile stacks the rows.

- [ ] **Step 7: Run** → PASS. **Commit**: `git commit -m "Make Settings General the factory status page"`

---

### Task 9: Settings · GitHub access and Repository setup

**Files:**
- Create: `ui/app/utils/repositories.ts`, `ui/app/components/ConnectionCard.vue`, `ui/app/components/RepositoryTable.vue`, `ui/app/components/RepositoryStage.vue`, `ui/app/pages/settings/github.vue`, `ui/app/pages/settings/repository.vue`
- Modify: `ui/server/utils/proxy.ts` and `ui/tests/unit/proxy.test.ts` (see Step 5, the ruleset download)
- Test: `ui/tests/unit/repositories.test.ts`, `ui/tests/e2e/settings-github.spec.mjs`, `ui/tests/e2e/repository-setup.spec.mjs`, fixtures `default/repositories.json`, `default/repositories.repo_id.json` (the `?repo_id=` variant), `needs-setup/repositories.repo_id.json`, `default/repositories-enable.post.json`, `default/settings.json` — all created in Task 5 from `tests/browser_repository_setup.cjs` and `browser_settings.cjs`.

**Interfaces:**
- Consumes: `SettingsView`, `RepositoriesView`, `RepositoryRow`, `ProtectionReport`; contracts §3.5–3.6 copy; `usePoll` (with `allowed`), `useForgeApi` (`get` with a 30 s timeout).
- Produces in `repositories.ts`: `ROLE_LABELS_APP` (Builder/Reviewer/Auditor/Controller), `ROLE_PURPOSES`, `STATUS_NAMES` (ready/ok/unknown/… → text from §3.5), `ACCESS_LABELS` (§3.6), `PROTECTION_LABELS` (§3.6 pill labels), `repoStage(row, activationAvailable): 0|1|2|3|4`, `repoDescription(row, live): string` (§3.6 description table), `rowState(row): { icon: 'attention'|'enabled'|'off'; label: string }` (protection counts as ready while `checking` with `protection_previous_status === 'ready'`), `connectionAction(role, roleAccess, onboardingRole): { label: string; href: string; external: boolean } | null`, `lastRefreshText(settings): string`.
- `RepositoryStage` props `{ stage: 0|1|2|3|4; activationAvailable: boolean }` renders the step strip "Repository › App access › Review rules › Enable/Finish setup" (the last label is "Enable" when `activationAvailable`, "Finish setup" otherwise) with the current step marked `aria-current="step"`. `settings/repository.vue` uses the component; the steps are not re-described inline on the page.
- Deliberately dropped: the contracts §3.5 dirty guards "Save or discard your edits before refreshing access." and "… before changing repository activation." — capacity editing no longer lives on `/settings/github` (it is on Settings · General, Task 8), so no dirty state can exist on this page and neither sentence can ever be true. Do not re-add them.

- [ ] **Step 1: Failing tests** for `repoStage` (0..4 cases), `rowState` (checking with previous ready → enabled), `repoDescription` (ready+canEnable sentence), `connectionAction` (installation_url → "Manage access ↗"; no app_id + install_url → "Install App ↗"; no app_id → "Connect in setup →" `/onboarding`), `lastRefreshText` (interrupted suffix).

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** with the copy from contracts §3.5–3.6. **Step 4: Run** → PASS.

- [ ] **Step 5: Pages.**
  - `settings/github.vue`: `useHead({ title: 'GitHub access' })` → "GitHub access · Forge" (the contracts' "Forge · GitHub access" is deliberately replaced by the new tab names). Connections grid of `ConnectionCard` per role (badge, identity, observation, action link, errors); refresh icon button → `/api/onboarding {action:'refresh'}` when phase is `setup` else `/api/settings/sync {}`; spinner while `sync.status === 'running'`. Refresh completion rules: it completes when `sync.status === 'complete'` with a new `finished_at` → "Access refreshed."; when `sync.status === 'interrupted'` or more than 90 s elapse → "The access check did not finish. Refresh access to try again."; a failed POST shows "Could not refresh access. Try again.". "Repositories" card with count, "Add repository" link, "Refresh repositories" → `/api/repositories/check {repo_id:null, revision}`, client-side search, `UTable` (Repository / Automation) with `rowState`, "Disable" → `/api/settings {config, revision}` (enabled:false) then "Settings saved."; help callout text verbatim.
  - `settings/repository.vue`: `useHead({ title: 'Prepare repository' })` → "Prepare repository · Forge" (same deliberate replacement). Reads `repo_id` from the query; `usePoll(() => api.get('/api/repositories' + query, { timeout: 30000 }), { interval: 3000, allowed: () => !busy.value })`; picker when no id (search "Find by name", list of non-enabled repos, "Repository missing?" collapsible); `RepositoryStage` strip; App access panel per role with `ACCESS_LABELS` and action links; summary with status pill and description; failed requirements list; download "Download ruleset" (`/api/repositories/ruleset?repo_id&revision`, `download` attr) or "Edit ruleset on GitHub ↗" when `edit_url`; hint text; footer: "Refresh repository" → `/api/repositories/check {repo_id, revision}`, "Last checked HH:MM", "Enable automation" → `/api/repositories/enable {repo_id, revision}` (the returned `RepositoriesView` replaces the snapshot), "Continue to Forge →" (`continue_url`) in setup mode; "Setup details" collapsible with the bypass note verbatim.
  - Auto-check rule, verbatim from contracts §3.6: when `access_ready` and `protection_status` ∈ unchecked/stale and the key `revision:protection_status:observed_at` has not yet been checked → call `check()`. Keep that dedupe key in component state so it fires once per observation; without it a page reached from "Check repository" sits on "Checking setup…" until the operator presses refresh.
  - Ruleset download: `/api/repositories/ruleset` answers with `Content-Disposition: attachment; filename="forge-review-rules-<id>.json"` (contracts §2), but Task 2's proxy passes only `['content-type','set-cookie','location','cache-control']`, which silently renames the file. Add `'content-disposition'` to `PASS_RESPONSE` in `ui/server/utils/proxy.ts` and extend the `responseHeaders` case in `ui/tests/unit/proxy.test.ts` to assert it survives.

- [ ] **Step 6: e2e** for both pages covering: badges and action links per role, refresh posts the right endpoint by phase, the three refresh-completion messages, Disable posts settings with `enabled:false`, picker → `?repo_id=`, the `RepositoryStage` strip marks the right step, needs-setup scenario shows the two failed requirements and the download link with the right `download` filename **and** the `Content-Disposition` header arriving through the proxy (assert via `page.waitForEvent('download')` or an in-page `fetch('/api/repositories/ruleset?repo_id=2').then(r => r.headers.get('content-disposition'))`), Enable posts `/api/repositories/enable` and the pill becomes "Enabled" from `repositories-enable.post.json` (`enabled: true`, `protection_status: 'ready'`), the auto-check fires exactly once for a stale `protection_status`, mobile layout.

- [ ] **Step 7: Run** → PASS. **Commit**: `git commit -m "Rebuild GitHub access and repository setup"`

---

### Task 10: Coding accounts, Onboarding and Reset

**Files:**
- Create: `ui/app/utils/onboarding.ts`, `ui/app/components/HarnessCard.vue`, `ui/app/components/HarnessDialog.vue`, `ui/app/components/SetupRail.vue`, `ui/app/pages/settings/harnesses.vue`, `ui/app/pages/onboarding.vue`, `ui/app/pages/reset.vue`, `ui/server/middleware/setup-cookie.ts`, `ui/server/routes/_ui/setup-cookie.get.ts`, `ui/app/middleware/phase.global.ts`
- Test: `ui/tests/unit/onboarding.test.ts`, `ui/tests/e2e/onboarding.spec.mjs`, `ui/tests/e2e/harnesses.spec.mjs`, fixtures `setup/onboarding.json`, `setup/onboarding-step3.json`, `setup/factory.json`, `setup/onboarding.post.json`, `resetting/onboarding.json`, `default/onboarding.json`, `provider-failed/onboarding.json` — all created in Task 5.

**Interfaces:**
- Consumes: `OnboardingView`, `Harness`, `SetupOperation`, contracts §3.7–3.8 copy; `usePoll` (with `allowed`), `useForgeApi` (`get` with a 30 s timeout).
- `setup-cookie.ts` (Nitro middleware): on **every GET whose path does not start with `/api/`** it sets `forge_setup=<existing or randomBytes(32).toString('base64url')>; Path=/; HttpOnly; SameSite=Lax; Max-Age=3600` when the request carries no valid one (`/[A-Za-z0-9_-]{32,128}/`; `base64url` of 32 bytes is exactly 43 characters). The proxy forwards it (Task 2).
- `ui/server/routes/_ui/setup-cookie.get.ts` exposes the same logic at `GET /_ui/setup-cookie` (Nitro auto-maps `server/routes/**`; the `_ui` prefix stays clear of `proxyPaths()`). `onboarding.vue`, `reset.vue` and `settings/harnesses.vue` call it on mount — a no-op when a valid cookie is already present. Without this a client-side route change from `/settings` to `/settings/harnesses` or `/onboarding` never reaches the middleware, and the next `POST /api/onboarding {action:'github'}` fails with "Choose a GitHub account type" (contracts §1, "Cookie `forge_setup`"), which reads to an operator as "Forge lost my account type".
- Produces in `onboarding.ts`: `initialStep(view): 0|1|2`, `harnessLine(h): string` ("Needs verification" / model lines with suffixes), `harnessState(h, activity): 'verified'|'verify'|'connect'|'busy'`, `step3(view, now): { title: string; intro: string; primary?: { label: string; action: 'check'|'start'|'link'; to?: string; help?: string }; secondary?: {...} }` implementing the state table in §3.8, `resetStep(operation): string`, `signature(view): string` (drops `service.last_seen`, adds `waitingService`).
- `HarnessCard` props `{ harness, activity }` — per-harness state only. `checks.provider`, `checks.runner` and `error` are page-level (contracts §3.7: "Error notice shown only when `checks.provider==='failed'` or `checks.runner==='failed'`"), so the error notice stays in `settings/harnesses.vue` and never moves into the card.

- [ ] **Step 1: Failing tests** for `initialStep`, `harnessLine`, `harnessState`, `step3` (waitingService, checking, ready, needsRules, not connected, failed, expired, default), `resetStep`, and `signature` (two views differing only in `service.last_seen` produce the same string; a view where `waitingService` flips produces a different one).

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** with the copy verbatim from contracts §3.7–3.8. **Step 4: Run** → PASS.

- [ ] **Step 5: Pages.**
  - `settings/harnesses.vue`: `useHead({ title: 'Coding accounts' })` → "Coding accounts · Forge" (the contracts' "Forge · Coding accounts" is deliberately replaced by the new tab names). Calls `GET /_ui/setup-cookie` on mount; polls `/api/onboarding` every 3 s with `allowed: () => !busy.value`; `HarnessCard` per harness (name, line, right side spinner/"✓ Verified"/button); `HarnessDialog` (`UModal`) with the two commands, copy buttons (`navigator.clipboard`, "Copied") and — when `navigator.clipboard` is undefined, which is the normal case because the UI is served over plain HTTP on the tailnet — the fallback text "Select and copy the command above" with the command pre-selected; "Account status updates automatically after verification."; error notice only when `checks.provider === 'failed' || checks.runner === 'failed'`, showing `error`, rendered by the page.
  - `onboarding.vue`: `useHead({ title: 'Setup' })` → "Setup · Forge" (same deliberate replacement). Calls `GET /_ui/setup-cookie` on mount. `SetupRail` steps; phase screens legacy/active/resetting; step 1 GitHub (role rows, owner + `USelectMenu` account type, Connect/Install/Continue, "Refresh access"; connect posts `{action:'github', role, owner, account_type}` and either navigates to `install_url` or submits a hidden form with field `manifest` to the returned `action`); step 2 repository radios; step 3 from `step3()` with the extras (selected repo panel, repository problem panel, coding accounts summary, technical checks, "How Forge will work"); `?connection_error` notice; auto `refresh` rule; navigate to `/` when phase becomes `active` after start.
  - Onboarding poll: `usePoll(() => api.get('/api/onboarding', { timeout: 30000 }), { interval: 3000, allowed: () => !busy.value && (Boolean(view.operation) || view.phase === 'resetting' || step.value === 3 || route.path === '/settings/harnesses') })` — the contracts §4 predicate, so a poll never lands while the operator is typing an owner name or choosing an account type.
  - Re-render only when the JSON signature changes: `const sig = computed(() => signature(poll.data.value)); watch(sig, () => rebuildStep3())`. `usePoll`'s own `onChange` is deliberately not used here — `service.last_seen` ticks every second and would reset the step-3 panels while the operator reads them. The `waitingService` threshold (an `operation` is present, the service is unavailable and more than 15 s have passed) is computed from `service.last_seen` against a 1 s ticker, paused when the document is hidden.
  - Open `UCollapsible` state and textarea contents live in component state keyed by harness id, so a poll cannot collapse a panel or clear a half-typed field.
  - `reset.vue`: `useHead({ title: 'Reset Forge' })` → "Reset Forge · Forge" (same deliberate replacement). Calls `GET /_ui/setup-cookie` on mount; the four-item list and note verbatim; "Cancel" → `/`; danger button → `/api/onboarding {action:'reset', generation}`; completion screen when the receipt/generation rules say so.
  - Route redirects, `ui/app/middleware/phase.global.ts`, using `useRequestFetch()` so the SSR fetch carries `Host` and `Cookie`: fetch `/api/onboarding`; on `/` redirect to `/onboarding` when the factory is not activated and onboarding is enabled **or** there are no repositories; on `/settings` redirect to `/onboarding` (303) when not activated; when `phase === 'resetting'` redirect **any** route other than `/onboarding`, `/reset` and `/setup-help` to `/onboarding`; and when any payload answers `{ok:false, reason:"Factory reset is in progress"}` (the contracts §1 GET guard 2 fence) redirect to `/onboarding` instead of letting the raw reason surface as a page error. This mirrors the Python 303s in contracts §2 "Pages/assets served".

- [ ] **Step 6: e2e**: onboarding step 1 connect posts the github action and submits the manifest form to the `action` returned by `setup/onboarding.post.json` (`http://127.0.0.1:8788/__manifest`, which the mock accepts with `application/x-www-form-urlencoded`, records, and answers `303 /onboarding`); step 2 selection posts `repository`; step 3 "Verify repository" posts `check` with the revision; resetting screen shows the step text; harnesses page lists three harnesses, "Connect" opens the dialog with both commands, copy works and the fallback text appears when `navigator.clipboard` is deleted; `/` redirects to `/onboarding` in the `setup` scenario (where `setup/factory.json` is `{ok:false, …}`, so the redirect middleware and the header pill both survive an unavailable factory); starting on `/settings` and navigating **client-side** to `/onboarding`, the recorded `POST /api/onboarding` carries `cookie` containing `forge_setup=` (E3's recorded `cookie` field proves Python receives it, not merely that Nitro set it); `ctx.scenario('resetting')` on `/issues` redirects to `/onboarding`.

- [ ] **Step 7: Run** → PASS. **Commit**: `git commit -m "Rebuild onboarding, reset and coding accounts"`

---

### Task 11: Help page rendered from the docs

**Files:**
- Create: `ui/server/routes/_ui/help.get.ts`, `ui/server/utils/help.ts`, `ui/app/pages/setup-help.vue`
- Test: `ui/tests/unit/help.test.ts`, `ui/tests/e2e/help.spec.mjs`, fixture `ui/tests/e2e/fixtures/help.md` (created in Task 5)

**Interfaces:**
- `renderHelp(markdown: string): { html: string; toc: { id: string; text: string; level: 1|2 }[] }` in `ui/server/utils/help.ts`: headings `#`–`###` with slug ids, paragraphs, `-` and numbered lists, 4-space code blocks, inline code, `[label](url)`; relative `*.md` links → `https://github.com/XRPL-Commons/forge/blob/main/docs/<file>`; non-http(s) links rendered as text; no raw HTML (sanitize-html with the same allowlist as Task 7, minus inputs).
- `GET /_ui/help` (Nitro auto-maps `server/routes/**`, and the `_ui` prefix stays clear of `proxyPaths()`) resolves the document in this order — `NITRO_HELP_DOC`, else `$FORGE_ROOT/docs/onboarding.md`, else the repo `../docs/onboarding.md` — taking the **first readable** one, because the installation copy at `$FORGE_ROOT/docs/onboarding.md` overrides the repo file (contracts §3.9). Returns `{ html, toc }`, cached for 60 s.

- [ ] **Step 1: Failing test**: heading ids `set-up-forge`, `github-repository-protection` produced; relative md link rewritten; `<img onerror>` stripped; `javascript:` link rendered as text; toc has h1/h2 only; resolution order — with `NITRO_HELP_DOC` set it wins over `FORGE_ROOT`, with only `FORGE_ROOT` set that copy wins over the repo file, and an unreadable `NITRO_HELP_DOC` falls through to the next candidate. One further test asserts the real `docs/onboarding.md` still produces the anchors `#host-installation` and `#github-repository-protection` — the two the UI deep-links — so the doc and the UI cannot drift apart silently.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Page**: `useHead({ title: 'Setup help' })` → "Setup help · Forge" (the contracts' "Forge · Setup help" is deliberately replaced by the new tab names); sticky sidebar "On this page" from `toc`; main with eyebrow "Help" and the html (`v-html` of sanitized output); deep links `#host-installation` and `#github-repository-protection` scroll with the header offset.
- [ ] **Step 6: e2e**: `run.mjs` serves the app with `NITRO_HELP_DOC=tests/e2e/fixtures/help.md`, so the spec asserts the seven headings of **that fixture** and never breaks when `docs/onboarding.md` is edited; `/setup-help#host-installation` lands on that heading.
- [ ] **Step 7: Commit**: `git commit -m "Render setup help from the docs on the server"`

---

### Task 12: Deployment units and switch-over runbook

**Files:**
- Create: `deploy/forge-ui.service.example`, `docs/ui-deployment.md`
- Modify: `deploy/forge-status.service.example` (add the two `Environment=` lines), `docs/installation.md` (link to the runbook), `ui/README.md` (build/test/run)

- [ ] **Step 1: `deploy/forge-ui.service.example`**

```ini
[Unit]
Description=forge-ui — tailnet-only operator UI (Nuxt/Nitro front door)
After=network-online.target forge-status.service
Wants=forge-status.service
[Service]
Type=simple
WorkingDirectory=%h/ops/forge-watchdog/ui
ExecStart=/usr/bin/node %h/ops/forge-watchdog/ui/.output/server/index.mjs
Environment=NODE_ENV=production
Environment=HOST=100.111.151.38
Environment=PORT=8787
Environment=NITRO_FORGE_API=http://127.0.0.1:8788
Environment=NITRO_HELP_DOC=%h/ops/forge-watchdog/docs/onboarding.md
Restart=always
RestartSec=5
[Install]
WantedBy=default.target
```
(`HOST` is the Tailscale IPv4; the runbook says to copy the value from `tailscale ip -4`.)

- [ ] **Step 2: `deploy/forge-status.service.example`** gains
```ini
Environment=FORGE_BIND_ADDRESS=127.0.0.1
Environment=FORGE_PORT=8788
```

- [ ] **Step 3: `docs/ui-deployment.md`** — the runbook:
```
1. cd ~/forger-remote/ui && npm ci && npm run build
2. rsync -a --delete ui/.output/ ~/ops/forge-watchdog/ui/.output/   (copy ui/ except node_modules)
3. cp deploy/forge-ui.service.example ~/.config/systemd/user/forge-ui.service; edit HOST
4. add the two Environment lines to ~/.config/systemd/user/forge-status.service
5. systemctl --user daemon-reload && systemctl --user restart forge-status && systemctl --user enable --now forge-ui
6. curl -s -o /dev/null -w '%{http_code}\n' http://<tailscale-ip>:8787/   → 200 (or 303 to /onboarding)
   curl -s http://<tailscale-ip>:8787/api/factory | head -c 80   → {"ok": true, …}
Rollback: systemctl --user disable --now forge-ui; remove the two lines; systemctl --user restart forge-status
```

- [ ] **Step 4: Verify on the box**: run steps 1–2 into a scratch root (`~/ops/forge-watchdog-ui-test`), start Nitro on `127.0.0.1:3999` with `NITRO_FORGE_API` pointing at the live Python on the Tailscale address (`Host` rewrite makes Python accept it), open `/`, `/issues`, `/settings`, `/setup-help` with curl and confirm 200 and real data; stop it. **Commit**: `git commit -m "Add the forge-ui service and the switch-over runbook"`

---

### Task 13: Remove the Python pages and the proxy fallback

**Files:**
- Delete: `legacy/dashboard.html`, `legacy/factory.html`, `legacy/issues.html`, `legacy/issues.js`, `legacy/onboarding.html`, `legacy/settings.html`, `legacy/repository-setup.html`, `legacy/repository-setup.js`, `legacy/markdown.js`, `legacy/vendor/`, `forge/navigation.py`, `forge/select_menu.py`, `forge/setup_help.py`, `forge/polling.py`, `forge/illustrations.py`, `tests/browser_*.cjs`, `tests/browser_page.cjs`, `tests/test_select_menu.py`, `tests/test_setup_help.py` (if present), root `package.json`
- Modify: `legacy/forge-status.py` (remove every HTML/asset route and the `PAGE*`/`FAVICON` constants; keep `/api/*` and `/onboarding/github/callback`; unknown paths 404), `ui/server/middleware/forge-proxy.ts` (`proxyPaths` no longer needed to change — the fallback was never added to it; delete any catch-all left from Task 2 experiments), `.github/workflows/ci.yml` (drop `npm run test:browser`), `docs/factory-runtime.md` and `docs/onboarding.md` (mention the UI service)
- Test: run `python3 -m unittest discover -s tests` and fix imports that referenced removed modules (`tests/test_navigation*.py`, `tests/test_setup_terminal.py` if it imported `render`).

- [ ] **Step 1: Delete the files and routes.** **Step 2: Run the Python suite** → PASS (adjusting only tests that tested the deleted pages). **Step 3: Run `ui` unit + e2e** → PASS. **Step 4: Commit**: `git commit -m "Remove the hand-written operator pages now served by the Nuxt UI"`

---

## Self-review

- Spec coverage: architecture (T1, T2, T12), proxy guard and header rules (T2, extended in T9 with `content-disposition`), screens table (T6–T11), redirects and the reset fence (T10 `phase.global.ts`), polling rules (T3, `allowed` and fetch timeouts added in T5), theme and kit (T1), testing (T5 + per task), deployment and rollback (T12), cleanup (T13), favicon moved to Nuxt (T1), recovery counted as working (T4, T6, T8).
- Redundancies removed per Luc: at most one message block above the metrics (T6, T8), no Output popover (T6/T7), GitHub links only in the detail while internal remediation links stay on the card footer (T6/T7).
- Consistency: `useForgeApi().get/act` (with `timeout`), `usePoll` return shape (with `allowed`), `warnings({ factory, onboarding }, now, currentPath)` (T6) as the single banner producer reused by T8, `CardModel` as the only card payload (T6) with `toCard()` for internal jobs, `WarningBanner` and `ConfirmDialog` shared (T7's `DecisionPanel` wraps the dialog), `renderMarkdown` (T7) reused by T11's allowlist, UI-owned routes under `/_ui/` (T7, T10, T11), one fixture naming rule (T5).
- Deliberate drops recorded in the tasks so a reviewer does not re-add them: the "Connection interrupted…" notice (T6), the `/settings/github` dirty guards (T9), and `ForgeRefresh.html`'s in-place DOM patching beyond keyed collapsible/textarea state (T7, T10).
