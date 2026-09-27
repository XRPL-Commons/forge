# Nuxt operator UI — design

Date: 2026-09-22. Status: approved by Luc (with: favicon served by Nuxt; rationalize and simplify redundancies as we go).

## Goal

Replace the hand-written HTML operator pages with a Nuxt 4 application that renders server-side, keeps the Python supervisor and status server as the only writers of Forge state, and rebuilds every screen around issues #26, #27 and #28. One URL, one front door, no second port.

## Non-goals

- Rewriting Python reads or writes in JavaScript (a later decision; see "Later").
- GitHub sign-in for operators (issue #15).
- The retired pre-Factory-v2 dashboard and its endpoints (`/api/status`, `/api/workflow`, `/api/history`, `/api/repository`, `/api/pane`).

## Architecture

```
browser ──tailnet──> Nitro (ui/.output, binds <tailscale-ip>:8787)
                       ├─ SSR pages (Nuxt 4 + Nuxt UI v4)
                       ├─ /api/**  ──proxy──> Python status server 127.0.0.1:8788
                       └─ /onboarding/github/callback ──proxy──> same
```

- `ui/` lives in XRPL-Commons/forge. The runner image and the Python package are untouched by the build; only the control host gains a Node 24 runtime for Nitro.
- The Python status server keeps every JSON endpoint and the GitHub App callback. It moves to localhost with two environment variables on its unit: `FORGE_BIND_ADDRESS=127.0.0.1`, `FORGE_PORT=8788`.
- Nuxt serves `/favicon.svg` itself (static asset). Python's favicon route becomes dead and is deleted with the other HTML routes in the cleanup commit.

## Proxy layer (`ui/server/`)

One Nitro handler forwards to Python:

- Paths: `/api/**`, `/onboarding/github/callback`. During the switch a fallback forwards any other unmatched path too; the fallback is removed in the cleanup commit.
- Request rewriting: `Host` → `127.0.0.1:8788`; `Origin` → `http://127.0.0.1:8788` on POST; forward `Cookie`, `X-Forge-Action`, `Content-Type`, body; forward `Sec-Fetch-Site: same-origin` only after the edge guard below passed.
- Response: status, `Content-Type`, `Set-Cookie`, `Location`, `Cache-Control` passed through; bodies streamed; response size bounded (1 MiB) and timeout 30 s.
- Edge guard for POST, mirroring Python's rules so a forged request never reaches it: `Origin` equals `http://<Host>` of the Nitro request, `Sec-Fetch-Site` is `same-origin` (or absent for non-browser clients on the tailnet is NOT accepted: header required), `Content-Type` is `application/json`, `X-Forge-Action` header present, body ≤ 256 KiB. Python still validates the token value; Nitro never learns or stores it.
- The capability token: every Python GET payload carries `controls_token`; a composable keeps the latest value client-side and sends it as `X-Forge-Action` on actions, exactly as the current pages do.

## Screens

Shared layout: brand, work tabs (Activity, Issues), Settings, Help; a live activity pill fed by `/api/factory` (working counts per role, queued count). Recovery workers count as working everywhere they are shown (fixes the "queued with nothing working" confusion).

| Route | Data | Notes |
|---|---|---|
| `/` (Activity) | GET `/api/factory`, GET `/api/factory/output`, POST `/api/factory/action` | Issue #26: one `IssueCard` component, fixed height, title clamped, one primary action "Open" → `/issues/<key>`, compact Stop with confirmation. Issue #27: one `WarningBanner` per warning with a single destination button; no repeated heading. Stage columns, Needs attention, Finishing, Investigations all use `IssueCard`. |
| `/issues`, `/issues/<key>` | GET `/api/issues`, POST `/api/issues/action`, POST `/api/issues/refresh` | Filters with `USelectMenu`; detail as a route (works as a shared link) with decisions, links, attempts, holds, and a collapsed "Worker output" section (`/api/factory/output`). |
| `/settings` (General) | GET/POST `/api/settings`, GET `/api/onboarding`, GET `/api/factory` | Issue #28: status summary (dispatch, image + qualification, coding accounts with pass/fail sentence, GitHub access, last observation), warnings as banners, capacity with live counts, Reset at the bottom. |
| `/settings/github`, `/settings/repository` | `/api/repositories`, `/api/repositories/ruleset`, `/check`, `/enable`, `/api/settings/sync` | Ported from `repository-setup.js`; same flows. |
| `/settings/harnesses` (Coding accounts) | GET `/api/onboarding` (harness list), login/check commands | Shows every installed harness (Codex, Claude Code, Grok Build) with status and terminal instructions. |
| `/onboarding`, `/reset` | GET/POST `/api/onboarding`, callback proxied | Ported step flow; cookie flows through the proxy. |
| `/setup-help` | Nitro route renders `docs/onboarding.md` | `markdown-it` + sanitize on the server; no client-side markdown libraries. |

Polling: one composable (`usePoll`) with the current rules — coalesced requests, backoff to 15 s on failure, pause when the tab is hidden, retain the last good payload and show a "stale" indicator.

Redundancy rules while porting: one component per concept (card, banner, status row, confirm dialog); a message shown once per page; no duplicate headings; links to GitHub live in the detail view only.

## UI kit and theme

Nuxt UI v4 + Tailwind v4, dark only. Theme tokens in `ui/app/assets/css/main.css`: background `#101416`, panel `#192024`, line `#303b40`, text `#edf1ee`, muted `#acb7b9`, mint `#bfe6cd` (primary), blue `#abcfff`, amber `#ecc486`. Components: `USelectMenu` for every select, `UAlert` for banners, `UCard`, `UBadge`, `UModal`, `UTable`, `UButton`. No page-level custom CSS. Scoped styles belong in components that own sanitized Markdown typography (`MarkdownBody`, `HelpBody`) or card movement (`StageColumn`); these are explicit exceptions to the utility-only layout rule.

## Testing

- Vitest: proxy guard (rejects wrong Origin / missing header / wrong content type; rewrites Host/Origin; forwards cookies), `usePoll`, view helpers (warning → destination mapping, card clamping data).
- Playwright end-to-end: `ui/tests/e2e/*.spec.ts` against `nuxt build` output with a Node mock of the Python API (`ui/tests/mock-api.mjs`) that serves the fixtures adapted from the existing browser tests (issues, factory, onboarding, settings, repository setup, refresh lifecycle, mobile layout). Runs inside the runner image (`chromium` + `playwright-core`) as the current browser tests do.
- CI: a `ui` job (install, vitest, build, e2e in the runner image).

## Deployment

1. `cd ui && npm ci && npm run build` on the box (system Node 24).
2. `forge-ui.service` (user unit): `ExecStart=/usr/bin/node %h/ops/forge-watchdog/ui/.output/server/index.mjs`, `Environment=HOST=<tailscale-ip> PORT=8787 NITRO_FORGE_API=http://127.0.0.1:8788`, `Restart=always`.
3. `forge-status.service` gains `Environment=FORGE_BIND_ADDRESS=127.0.0.1` and `FORGE_PORT=8788`.
4. Order: stop `forge-status`, start it on localhost, start `forge-ui`. Rollback: stop `forge-ui`, remove the two environment lines, restart `forge-status`.
5. Cleanup commit after the switch: delete the Python HTML routes and pages (`legacy/*.html`, `issues.js`, `repository-setup.js`, `markdown.js`, `vendor/`), `forge/navigation.py`, `forge/select_menu.py`, `forge/setup_help.py`, the old `tests/browser_*.cjs`, and the root `package.json`; the Python server keeps only `/api/*` and the callback.

## Later (not in this slice)

Move reads into Nitro screen by screen (state files are written atomically, reads are safe), leaving Python with the action queue, settings validation, onboarding and credentials. Writes stay in Python until a cross-language lock story exists.
