# Operator UI fixtures

JSON fixtures for the Nuxt operator-UI end-to-end test harness, served by
`tests/e2e/mock-api.mjs`. Sourced from the Playwright fixtures in
`tests/browser_*.cjs` and the payload shapes in
`docs/superpowers/specs/2026-09-22-operator-api-contracts.md` §2, then renamed
onto the `mock-api.mjs` naming rule (path segments after `/api/` joined with
`-`; a query parameter that selects a specific fixture appends `.<param>`; a
POST body override appends `.post`) and extended for Task 5. Every fixture
uses `"controls_token": "synthetic"` and `observation.at` / timestamps anchored
to `1790000000` (treat this as "now" in tests) unless noted otherwise.
`controls_token` is injected by the mock into every object response whose top
level is not `{"ok": false, ...}` — including `factory-output.json` below, so
that field is omitted from the fixture file itself and added at serve time.

## default/

Baseline "everything healthy" scenario covering one issue per pipeline stage,
a running build job, a recovery job, a failed issue, an audited (finishing)
issue and a merged issue.

- `factory.json` — `GET /api/factory` for the Factory page (`/factory`,
  the Nuxt Factory page; ported from the removed `tests/browser_factory.cjs`).
  Contains repos `XRPL-Commons/academy` (id 1) and `XRPL-Commons/hackathon`
  (id 2); issues `1:601` (ready, running build job), `1:602` (built, queued
  for review), `2:603` (reviewed, queued for audit), `1:604` (failed,
  attention), `2:605` (audited, finishing), `2:606` (merged); a recovery job
  (`22222222-…`) investigating `1:604` without being referenced by any
  issue's `active` field; `runtime.slots {build:2,review:1,audit:1,recovery:1}`,
  `runtime.enabled:true`. Extended for Task 5's fixed-height card assertion
  with four more issues — `1:611` (ready, no job, no reason: a queued build
  issue), `1:612` (built, title >120 characters), `2:615` (ready,
  `prerequisite_hold:true`), `2:616` (failed, `failure_count:2`) — and a
  second recovery job (`33333333-…`, `issue_key:"3:900"`) that has no
  matching entry in `issues[]`. The existing `1:601` build job already
  carries both `provider` and `model`.
- `factory-output.json` — `GET /api/factory/output?id=` worker-output dialog
  on the Factory page. Matches the contract's literal shape `{ok, logs}`;
  the real endpoint does not return `controls_token`, but the mock injects
  it into every non-`{ok:false}` object response regardless (see above), so
  this response does carry one when served through `mock-api.mjs`.
- `issues.json` — `GET /api/issues` list response for the Issues page
  (`/issues`; ported from the removed `tests/browser_issues.cjs`). Six rows
  spanning every status
  (attention ×2, working, backlog, queued, completed) across both repo
  scopes; `body`/`history`/`reviews` stripped per the list contract.
- `issues.issue.json` — `GET /api/issues?issue=1:610` detail response (the
  "working / Building" row) for the issue detail side panel. `body` is a
  markdown sample exercising heading/list/relative-link/checkbox/`<script>`
  sanitization; `history` has 3 events (`discovered`, `assigned`,
  `operator_queue`).
- `settings.json` — `GET /api/settings` for Settings · General /
  GitHub access (ported from the removed `tests/browser_settings.cjs`). All
  four roles `ok`; one
  enabled+ready repo (academy), one not-enabled repo (hackathon).
- `onboarding.json` — `GET /api/onboarding`, phase `active` (post-setup
  steady state used by the header activity pill and `/settings/harnesses`).
  Codex and Claude connected with one passed model each; Grok not connected.
- `repositories.json` — `GET /api/repositories` for Settings · Repository
  setup (`/settings/repository`; ported from the removed
  `tests/browser_repository_setup.cjs`), both
  rows.
- `repositories.repo_id.json` — `GET /api/repositories?repo_id=2`, the
  single-row view for the not-yet-enabled hackathon repo, ready to enable.
- `repositories-ruleset.json` — `GET /api/repositories/ruleset?repo_id=&revision=`,
  the generated "Forge review baseline" ruleset from contracts §2. The mock
  serves it with `Content-Disposition: attachment; filename="forge-review-rules-<repo_id>.json"`,
  so the repository-setup spec can assert the download's filename end to end
  (Task 9).
- `runtime.post.json` — `POST /api/runtime` override: `factory.json` with
  `ok:true` and `runtime.slots.build` raised to `3`, so a runtime-slot save
  returns the full `FactoryView` the page needs instead of a bare `{ok:true}`
  that would blank `runtime`.
- `repositories-enable.post.json` — `POST /api/repositories/enable` override:
  `repositories.json` with `ok:true` and the hackathon row (`id:2`) flipped
  to `enabled:true`, `protection_status:"ready"`.

## warning/

- `factory.json` — default factory with a supervisor observation error
  (`1:824`, phase `github`) surfaced as the "Forge needs attention" operator
  banner. Exercises the Factory page's notice line / operator status logic.

## provider-failed/

- `onboarding.json` — default onboarding with Claude's model verification
  `"failed"`, `checks.provider:"failed"`, and the sandbox-verification error
  string. Exercises the Coding accounts page's error notice and Step 3's
  "Setup needs attention" branch.

## needs-setup/

- `repositories.repo_id.json` — repo 2 (hackathon) with
  `protection_status:"needs_setup"`; the `reviews` and `stale` protection
  requirements fail using the exact contract copy ("At least two approving
  reviews" / "Require pull requests with at least two approving reviews.",
  "Fresh approvals when code changes" / "Dismiss stale pull request approvals
  when new commits are pushed."). No `edit_url` (no existing Forge ruleset),
  `rules_url` set. Exercises the "download ruleset / import on GitHub" steps
  on Settings · Repository setup.

## needs-apps/

- `settings.json`, `onboarding.json` — a factory still being set up, with one
  App per connection state: `build` has stored credentials and a canonical
  installation URL ("Manage access ↗"), `review` is registered on GitHub but
  absent from `factory.json` ("Installation unverified" + "Install App ↗"),
  `audit` has credentials but missing permissions, `controller` was never
  connected ("Connect in setup →"). `sync:{"status":"never"}`, onboarding
  phase `setup`. Exercises every badge and action link on Settings · GitHub
  access, and its refresh going to `POST /api/onboarding {action:'refresh'}`
  instead of `POST /api/settings/sync` (Task 9).

## refreshed/

- `settings.json` — `default/settings.json` with
  `sync:{"status":"complete","finished_at":1790000600}`, a newer timestamp
  than the default. Switching to it after an access refresh is what the page
  observes as "Access refreshed." (Task 9).

## interrupted/

- `settings.json` — `default/settings.json` with
  `sync:{"status":"interrupted","started_at":1790000600}`: the access refresh
  the supervisor gave up on ("The access check did not finish. Refresh access
  to try again." plus the status line's " · Refresh interrupted; the previous
  data is shown.", Task 9).

## sync-failed/

- `settings-sync.post.json` — `POST /api/settings/sync` refused with a bare
  `{"ok": false}` (no reason), so the page falls back to its own sentence
  "Could not refresh access. Try again." (Task 9).

## stale/

- `repositories.repo_id.json` — `default/repositories.repo_id.json` with
  `protection_status:"stale"`, `protection_ready:false`: a report older than
  the access max age. Exercises Settings · Repository setup's automatic
  re-check, which must fire exactly once per
  `revision:protection_status:observed_at` observation (Task 9).

## setup/

- `onboarding.json` — phase `setup`, Step 1 (GitHub) state: build/review
  `ok`, audit registered but `not_configured`, controller unregistered,
  no repository selected, no provider connected. Exercises the Onboarding
  page's Step 1 GitHub-connection rows.
- `factory.json` — `{"ok": false, "reason": "Factory state is unavailable;
  reconcile the durable ledger."}`, the realistic pre-activation response.
  `/` runs both the redirect middleware and the header pill against
  `/api/factory` in this scenario.
- `onboarding.post.json` — `POST /api/onboarding` override for the GitHub
  App manifest hand-off: `{"ok": true, "action":
  "http://127.0.0.1:8788/__manifest", "manifest": {"name": "forge-build"}}`.
  The app is expected to submit the returned `manifest` to `action`
  (`mock-api.mjs`'s `POST /__manifest`), which redirects to `/onboarding`.

## setup-step3/

- `onboarding.json` — phase `setup`, all four roles `ok`, repository
  `XRPL-Commons/academy` selected, provider connected, but `checks:{}` and
  `can_start:false` (verification not yet run); includes the selected repo's
  `repository_setup` row (protection ready). Exercises Onboarding Step 3
  ("Verify your repository") default/expired branches, and Step 2's
  repository radio rows. Written for Task 5 as `setup/onboarding-step3.json`
  and moved here in Task 10: `mock-api.mjs` derives the fixture name from the
  request path (`/api/onboarding` -> `onboarding`), so the only way to serve a
  second onboarding payload is a second scenario directory.

## setup-fresh/

- `onboarding.json` — phase `setup` before anything exists: no owner, all four
  roles unregistered and `not_configured`, no repositories, no provider. The
  Onboarding Step 1 state that asks for the GitHub account name and account
  type, and the only one whose primary button reaches the App-manifest
  hand-off (a role that is already registered navigates to its `install_url`
  instead).
- `onboarding.post.json` — same payload as `setup/onboarding.post.json`, so
  `POST /api/onboarding {action:'github'}` answers with the manifest and its
  `action` URL in this scenario too.

## legacy/

- `onboarding.json` — `default/onboarding.json` with `phase:"legacy"`: a
  pre-v2 factory is still running, so guided setup offers a reset and
  nothing else. `/` redirects here like every other non-active phase.

## resetting/

- `onboarding.json` — phase `resetting` with an in-flight reset
  `operation` (`step:"stop_workers"`), using the reduced shape the contract
  specifies for this phase (roles all `not_configured`/unregistered, no
  repositories, no selection, empty `revision`). Exercises the Onboarding
  "Reset in progress" screen and its `operation.step` spinner copy.

## no-repos/

- `issues.json` — no repositories configured, no issues, `complete:true`.
  Exercises the Issues page's "Choose where Forge should work" empty state.
  (Renamed from `empty/` per Task 5 Step 5 — E8's "no repositories" scenario.)

## paused/

- `factory.json`, `issues.json`, `settings.json` — each exactly
  `{"ok": false, "reason": "Factory reset is in progress"}`, the reset fence
  from contracts §1's GET guard 2. Tasks 6–8 each assert their page's
  behaviour when the factory is paused mid-reset.

## stop-rejected/

- `factory.json` — `default/factory.json` plus `actions: [{"id": "r1",
  "action": "stop", "target": "11111111-1111-4111-8111-111111111111",
  "reason": "Stuck", "status": "rejected", "error": "Worker already
  stopped"}]`, so the Factory page's rejected-action banner can be
  exercised against the running build job.

## moved/

- `factory.json` — `default/factory.json` with issue `1:601` moved from
  `state:"ready"` to `state:"built"` (the same issue one column further
  right), for the Factory page's column-move animation and its
  `aria-live` "Issue 601 moved to Review" announcement. Its build job
  (`11111111-…`) is finished with it — `outcome:"accepted"`,
  `cleanup:true`, and the issue's `active` cleared — because an issue with
  a live job belongs to that job's column, not to its state's.

## decision-pending/

- `issues.issue.json` — `default/issues.issue.json` with a decision for
  `1:610` waiting for the supervisor: the response-level `actions` carries
  `[{"id": "a1", "key": "1:610", "status": "pending", "message": null}]`
  (the shape contracts §2 gives `IssueListResponse`/`IssueDetailResponse`),
  and the issue itself is `status:"waiting"`, `actions: []`, explanation
  "Applying your decision. Forge will confirm it before continuing." — what
  `presentation()` reports while an `operator_request` is pending. Reshaped
  in Task 7: it previously put an `IssueActionStatus` array in
  `issue.actions`, where the contract has the issue's own string actions,
  and keyed it to an issue the fixture does not describe.
- `issues.json` — `default/issues.json` with the same pending decision and
  the `1:610` row moved to `waiting`, so the list's "Applying…" pill is
  exercised against the same state as the detail. Added in Task 7.

## decision-queue/ and decision-pause/

The mock serves one `issues.issue.json` per scenario — the `issue=<key>`
query selects the endpoint, not the payload — so a detail fixture can only
offer the decisions of one issue state. These two carry the detail payloads
of two rows already in `default/issues.json`, so Task 7's e2e spec can
exercise a decision that needs no reason and one that does:

- `decision-queue/issues.issue.json` — `1:609`, the backlog row
  (`status:"backlog"`, `actions:["queue"]`, no active job): the only state
  that offers `queue`.
- `decision-pause/issues.issue.json` — `1:608`, the queued row
  (`status:"queued"`, `actions:["backlog","pause"]`): the only state that
  offers `pause`. It is also the one detail fixture with
  `body_truncated: true`, so the issue's "Description shortened…" note is
  exercised.

## orphan-job/

The issue behind a `?job=<id>` link has left the inventory, but its worker
output has not.

- `issues.issue.json` — the bare `{ok:false, reason:"This issue is not in a
  managed repository. Refresh the issue list."}` shape, served verbatim with
  status 200, so the detail fails while the page still has a job id from the
  query.
- `factory-output.json` — the log that `?job=` points at, distinct from
  `default/`'s so the spec can tell which scenario answered.

## down (not a fixture directory)

`down` is a scenario name recognized directly by `mock-api.mjs`, not a
fixture directory: while active, every `GET /api/**` has its socket
destroyed instead of answered, simulating the upstream being unreachable so
the Nitro proxy's `fetch()` rejects and returns 502. Used by
`smoke.spec.mjs` to assert the proxy's exact 502 reason string.

## help.md (not scenario-namespaced)

A fixed help document (exactly seven `#`/`##` headings, including
`## Host installation` and `## GitHub repository protection`) served in
place of the live `docs/onboarding.md` — `run.mjs` points the app at it via
`NITRO_HELP_DOC=tests/e2e/fixtures/help.md` so help specs never depend on
the real document's content (Task 11).

## Validation

Every file was validated with:

```
node -e "JSON.parse(require('fs').readFileSync(process.argv[1],'utf8'))" <file>
```

- `check-rejected/` — `stale` row plus a refused `POST /api/repositories/check` (`{ok:false}`); repository-setup asserts the auto-check is retried once checks are accepted again.
