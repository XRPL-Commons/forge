# Forge operator UI

Nuxt 4 app that replaces the legacy static dashboard. It talks to the Forge
Python API only through its own Nitro server, which proxies `/api/**` and
`/onboarding/github/callback` to the upstream configured by `NITRO_FORGE_API`
(see `server/middleware/forge-proxy.ts`).

The Nitro server also mints the `forge_setup` cookie that Python reads back
on `POST /api/onboarding {action:'github'}` and on the GitHub callback
(contracts §1): `server/middleware/setup-cookie.ts` sets it on every GET
outside `/api/**`, and `server/routes/_ui/setup-cookie.get.ts` exposes the
same no-op-if-present logic at `GET /_ui/setup-cookie`, which Onboarding,
Reset and Coding accounts call on mount — a client-side route change into
those pages never reaches the middleware.

## Develop

```
npm install
npm run dev
```

## Unit tests

```
npm test
```

Runs `vitest` over `tests/unit/**` (composables, proxy guard, formatting
utilities). No network or browser required.

## End-to-end tests

```
npm run test:e2e
```

`tests/e2e/run.mjs`:

1. Builds the app with `npm run build` if `.output/` is missing.
2. Starts `tests/e2e/mock-api.mjs` on `127.0.0.1:8788` — a mock stand-in for
   the Python Forge API, serving fixtures from `tests/e2e/fixtures/` and
   switchable per-scenario at runtime (`POST /__scenario {name}`).
3. Starts the built app on `127.0.0.1:3999` with `NITRO_FORGE_API=http://127.0.0.1:8788`,
   `NUXT_PUBLIC_POLL_INTERVAL=300` (so poll-driven assertions take ~300ms
   instead of 5s) and `NITRO_HELP_DOC=tests/e2e/fixtures/help.md`.
4. Runs every `tests/e2e/*.spec.mjs` file with `playwright-core` against a
   real Chromium, one browser context per spec.
5. Exits non-zero if any spec failed.

A spec is a file exporting `default async function (page, ctx)`, where
`ctx = { base, scenario(name), requests }`:

- `base` — the running app's origin (`http://127.0.0.1:3999`).
- `scenario(name)` — switches the mock's active fixture scenario and clears
  its request log.
- `requests()` — resolves the mock's recorded request log
  (`GET /__requests`): `{ method, path, query, body, cookie }[]`.

### Env vars for `test:e2e`

- `FORGE_BROWSER_PATH` — Chromium executable to launch. Empty/unset falls
  back to playwright-core's own bundled Chromium (install it first with
  `npx playwright-core install chromium`).
- `FORGE_PLAYWRIGHT_MODULE` — module specifier/path to load `chromium` from
  instead of this project's own `playwright-core` dependency (e.g. a
  system-installed copy in a locked-down runner image).
- `FORGE_CHROMIUM_SANDBOX` — set to `0` to launch with `chromiumSandbox:
  false`, for local runs on hosts where the Chromium sandbox can't be set up
  (this has been needed on some macOS setups). Leave unset in CI and the
  runner image, which keep the default `chromiumSandbox: true`.

Example local run (macOS, playwright's own downloaded Chromium):

```
FORGE_BROWSER_PATH=/Users/you/Library/Caches/ms-playwright/chromium-XXXX/chrome-mac-arm64/"Google Chrome for Testing.app"/Contents/MacOS/"Google Chrome for Testing" \
FORGE_CHROMIUM_SANDBOX=0 \
npm run test:e2e
```

(Different playwright-core versions bundle either a `Chromium.app` or a
`Google Chrome for Testing.app`; use whichever exists under the newest
`chromium-*` directory in `~/Library/Caches/ms-playwright`.)

### Running inside the runner image

```bash
cd ~/forger-remote/ui && npm run build
docker run --rm --network none --security-opt seccomp=unconfined --tmpfs /private/tmp -v ~/forger-remote:/src:ro -w /src/ui \
  -e HOME=/tmp -e FORGE_BROWSER_PATH=/usr/bin/chromium -e FORGE_PLAYWRIGHT_MODULE=/usr/local/lib/node_modules/playwright-core \
  forge-runner:local node tests/e2e/run.mjs
```

Expected output: `PASS smoke`. The `.output` bundle is self-contained;
`node_modules` are not needed inside the image except `playwright-core`,
which the image provides at `/usr/local/lib/node_modules/playwright-core`.

### CI

The `ui` job installs playwright-core's bundled Chromium
(`npx playwright-core install chromium --with-deps`) and runs
`npm run test:e2e` with `FORGE_BROWSER_PATH` unset, so `run.mjs` falls back
to that bundled Chromium.

## The mock API (`tests/e2e/mock-api.mjs`)

An `http` server standing in for the real Python Forge API:

- `GET /api/<x>` returns `fixtures/<scenario>/<name>.json` when present,
  else `fixtures/default/<name>.json`; 404 `{ok:false, reason:'missing'}`
  when neither exists.
- `POST /api/<x>` validates `X-Forge-Action === 'synthetic'`,
  `Origin === 'http://127.0.0.1:8788'` and `Sec-Fetch-Site === 'same-origin'`
  (already rewritten/enforced by the Nitro proxy in front of it), then
  answers `{ok:true}` unless `fixtures/<scenario>/<name>.post.json` exists,
  in which case that file is served verbatim.
- `controls_token: 'synthetic'` is injected into every object response
  whose top level is not already `{"ok": false, ...}` — a `{ok:false}`
  fixture is served verbatim with HTTP status 200, so the
  `{ok:false, reason}` path is exercised the same way the real API does it.
- `GET /api/repositories/ruleset` additionally sends
  `Content-Disposition: attachment; filename="forge-review-rules-<repo_id>.json"`.
- `POST /__manifest` — the GitHub App manifest hand-off stand-in: accepts
  any content type, skips the action guard, records the request and
  replies `303` with `Location: /onboarding`.
- `GET /onboarding/github/callback` — always replies `303` with
  `Location: /onboarding` and `Set-Cookie: forge_setup=<43-character
  token>; Path=/; HttpOnly`, so the proxy's cookie pass-through can be
  exercised. The value stands in for Python's `secrets.token_urlsafe(32)`:
  a shorter one would be replaced by `server/middleware/setup-cookie.ts` on
  the `/onboarding` this redirect leads to, exactly as the contracts say.
- Scenario `down` (switch to it with `ctx.scenario('down')`): every
  `GET /api/**` has its socket destroyed instead of answered, simulating
  the upstream being unreachable — the Nitro proxy's `fetch()` rejects and
  the app sees a 502 with its exact reason string.
- Every GET/POST request is recorded as `{method, path, query, body,
  cookie}` and exposed at `GET /__requests`; `POST /__scenario {name}`
  switches scenario and clears the log.

### Fixture naming rule

Path segments after `/api/` join with `-`: `/api/factory/output` →
`factory-output.json`. A query parameter that selects a specific fixture
appends `.<param>`: `/api/issues?issue=` → `issues.issue.json`,
`/api/repositories?repo_id=` → `repositories.repo_id.json`. A POST body
override appends `.post`: `/api/runtime` → `runtime.post.json`,
`/api/repositories/enable` → `repositories-enable.post.json`. See
`tests/e2e/fixtures/README.md` for what each fixture/scenario covers.

## Runtime config

| Key                         | Env override               | Default                    |
| ---------------------------- | --------------------------- | --------------------------- |
| `forgeApi`                   | `NITRO_FORGE_API`            | `http://127.0.0.1:8788`     |
| `helpDoc`                    | `NITRO_HELP_DOC`             | `../docs/onboarding.md`     |
| `public.pollInterval`        | `NUXT_PUBLIC_POLL_INTERVAL`  | `5000`                      |

## Build and run in production

```
npm run build       # writes the self-contained Nitro bundle to .output/
node .output/server/index.mjs
```

`.output/` bundles its own server code and dependencies; `node_modules` is
not needed alongside it. Configure `HOST`, `PORT`, `NITRO_FORGE_API` and
`NITRO_HELP_DOC` as environment variables (see the table above and
`deploy/forge-ui.service.example`). No runtime dependency on the Iconify
API: Nuxt UI's own `i-lucide-*` icons are bundled into `.output/` by
`icon: { serverBundle: 'local' }` plus the pinned `@iconify-json/lucide`
dev dependency, and `/api/_nuxt_icon/**` (excluded from `proxyPaths()`) is
answered by Nitro from that bundle.

To switch a running host from the legacy Python dashboard to this UI, see
[`docs/ui-deployment.md`](../docs/ui-deployment.md).
