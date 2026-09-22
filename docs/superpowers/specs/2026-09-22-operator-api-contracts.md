# Operator API contracts and page behaviors (for the Nuxt rebuild)

Date: 2026-09-22. Source of truth: `legacy/forge-status.py` (server), `forge/factory/control.py`, `forge/factory/issues.py`, `forge/factory/issue_actions.py`, `forge/factory/operator_status.py`, `forge/factory/ledger.py`, `forge/factory/config.py`, `forge/factory/github.py`, `forge/onboarding.py`, `forge/setup_service.py`, `forge/repository_setup.py`, `forge/settings.py`, `forge/setup_help.py`, `forge/navigation.py`, `forge/polling.py`, the legacy pages (`legacy/factory.html`, `legacy/issues.js`, `legacy/issues.html`, `legacy/settings.html`, `legacy/repository-setup.js`, `legacy/repository-setup.html`, `legacy/onboarding.html`, `legacy/markdown.js`) and the browser fixtures in `tests/browser_*.cjs`.

Conventions: types are TypeScript-style. `number` timestamps are Unix seconds (float) unless noted. Fields marked "(unverified)" were not confirmed end to end. Copy inside quotes is exact user-facing text.

---

## 1. Request rules

### Server
- `ThreadingHTTPServer` bound to one IPv4 address (Tailscale IP or `FORGE_BIND_ADDRESS`) and port (`FORGE_PORT`, default 8787).
- Every response from `_send` has status **200**, `Cache-Control: no-store`, `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff`. Logical failures are `200` with body `{ok:false, reason}`. Pages must therefore check `value.ok`, not only `response.ok`.
- HTML pages get the shared header injected server side (`forge.navigation.render`, see section 5).

### GET guards
1. `Host` header must equal the bound `address:port` exactly (IPv6 form `[addr]:port`), else **403** (DNS-rebinding defence). No `Origin` requirement on GET.
2. If `state/factory-paused.json` exists (reset in progress) every `/api/*` path except `/api/onboarding` returns `{ok:false, reason:"Factory reset is in progress"}`.
3. Unknown path → **404**. Query parameters are validated per route (see endpoint sections); invalid queries return `{ok:false, reason}` (issues/repositories/factory) or **400** (`/api/workflow`, `/api/history`, `/api/repository`, `/api/pane` — legacy dashboard only, not used by the operator pages).

### POST guards (in order)
1. `Host` trusted as above → else **403**.
2. Path must be one of: `/api/slots/action`, `/api/settings`, `/api/settings/sync`, `/api/factory/action`, `/api/runtime`, `/api/onboarding`, `/api/repositories/check`, `/api/repositories/enable`, `/api/issues/action`, `/api/issues/refresh` → else **404**.
3. `X-Forge-Action` header must equal (constant-time compare) the process-wide `controls_token`; `Origin` must parse as scheme `http`, netloc equal to `Host`, and no path/query/fragment; `Sec-Fetch-Site` if present must be `same-origin` (absent is accepted) → else **403**.
4. `Content-Type` (before `;`) must be `application/json` and no `Transfer-Encoding` header → else **415**.
5. `Content-Length` must satisfy `0 < length <= max` where max = **262144** for `/api/settings` and `/api/runtime`, **8192** for all other POSTs. Otherwise `{ok:false, reason:"invalid_request_size"}`.
6. Body is `json.loads` of exactly that many bytes. Shape validation is per route; any `ValueError`/`TypeError` → `{ok:false, reason:<message>}`; `OSError`/`ImportError`/`SubprocessError` → `{ok:false, reason:"Operation unavailable; check server configuration"}`.
7. For every POST other than `/api/onboarding`: if `state/factory-paused.json` exists → `{ok:false, reason:"Factory reset is in progress"}`.
8. For `/api/settings` and `/api/runtime`: if `state/onboarding/setup.json` exists and its `phase != "active"` → `{ok:false, reason:"Use guided setup until the factory has started"}`.

### controls_token flow
- `CONTROLS_TOKEN = secrets.token_urlsafe(32)`, generated once per server process. Every GET JSON payload the pages consume includes `controls_token: string` (`/api/factory`, `/api/issues`, `/api/settings`, `/api/onboarding`, `/api/repositories`). It is also echoed in `/api/settings` POST, `/api/runtime` POST and `/api/repositories/enable` POST responses.
- Pages read it from their last GET and send it as `X-Forge-Action` on every POST. A server restart rotates it; pages recover because the next poll delivers the new one.
- `/api/status` (legacy dashboard) sets `controls_token: null` when slot actions are unavailable; the factory pages never see that.

### Cookie `forge_setup`
- Set by GET `/onboarding`, `/reset`, `/settings/harnesses`: `forge_setup=<token>; Path=/; HttpOnly; SameSite=Lax; Max-Age=3600`. Value is the existing cookie if it matches `[A-Za-z0-9_-]{32,128}`, else a fresh `secrets.token_urlsafe(32)`.
- Read (`_browser()`) by POST `/api/onboarding` action `github` (required: "Choose a GitHub account type" error if missing) and by GET `/onboarding/github/callback`. The App-manifest challenge stores `sha256(cookie)` and the callback must arrive from the same browser.
- All page fetches use `credentials:'same-origin'` (default) so the cookie rides along.

### Client conventions worth preserving
- Fetch timeouts: factory page `AbortSignal.timeout(15000)`; issues, settings, repository setup `30000`; header activity pill `10000`; onboarding uses none.
- `cache:'no-store'` on issues, repository-setup and onboarding fetches.
- Error surfaces: pages throw `Error(value.reason || <page default>)` when `!response.ok || !value.ok`; HTTP failures become `` `Request failed (${status})` `` on factory/settings.

---

## 2. Endpoints

### Shared types

```ts
type Role = 'build' | 'review' | 'audit' | 'controller';          // GitHub App roles
type JobRole = 'build' | 'review' | 'audit' | 'recovery' | 'merge'; // execution roles
type IssueState = 'ready' | 'built' | 'reviewed' | 'audited' | 'approval_needed'
  | 'investigating' | 'hold' | 'failed' | 'merged' | 'cancelled';
type Failure = { ok: false; reason: string };

// forge/settings.py Settings.view()['roles'][role]
type RoleAccess = {
  status: 'ok' | 'unknown' | 'not_configured' | 'stale' | 'missing_permissions';
  missing_permissions: string[];                // e.g. "contents: write"
  required_permissions: Record<string, 'read' | 'write'>;
  reason: string | null;                        // AccessError text or "Access sync failed; retry or inspect server configuration"
  observed_at: number | null;
  installation_url: string | null;              // https://github.com/(organizations/<org>/)?settings/installations/<id>
  app_slug: string | null;
};

// forge/factory/github.py protection_report()
type ProtectionSource = { kind: 'classic' | 'ruleset'; name: string; url: string; id?: number | null; source_type?: string | null };
type ProtectionRequirement = { id: 'readable' | 'reviews' | 'stale'; label: string; passed: boolean; detail: string; sources: ProtectionSource[] };
type ProtectionReport = {
  repo_id: number; branch: string | null; observed_at: number; available: boolean;
  requirements: ProtectionRequirement[];        // only [readable] when !available
  sources: ProtectionSource[];
  errors: { source?: 'classic' | 'rulesets' | 'reviews'; detail: string }[];
  additional_rules: { type: string; source: ProtectionSource }[];
  code_owner_reviews: boolean;
  bypass: { status: 'operator_review'; detail: string };
  required_approvals?: number;
  edit_url?: string;                            // https://github.com/<repo>/settings/rules/<id> when a "Forge review baseline" ruleset exists
};
// Requirement copy: reviews -> label "At least two approving reviews", detail "<n> approvals required." | "Require pull requests with at least two approving reviews."
// stale -> label "Fresh approvals when code changes", detail "Approvals are dismissed when new commits are pushed." | "Dismiss stale pull request approvals when new commits are pushed."
// readable -> label "Check repository review rules", detail = first error detail.
// Error details: "Branch protection could not be read. Check Controller access and try again." / "Repository rules could not be checked. Check GitHub access and try again." / "GitHub returned incomplete review settings. Check again." / (repository_setup.check) "Could not check this repository. Refresh GitHub access and try again." / "All four Apps need access. Refresh GitHub access first."

// forge/settings.py + forge/repository_setup.py RepositorySetup.views()
type RepositoryRow = {
  id: number; name: string;                     // owner/name
  enabled: boolean; configured: boolean;        // configured = present in factory.json
  access: Record<Role, 'ready' | 'missing_access' | 'name_changed' | 'archived' | RoleAccess['status']>;
  access_ready: boolean;                        // all four roles 'ready'
  dispatch_allowed: boolean;                    // enabled && access_ready && protection_ready
  protection_status: 'unchecked' | 'checking' | 'unknown' | 'stale' | 'ready' | 'needs_setup' | 'access_needed';
  protection_ready: boolean;                    // protection_status === 'ready'
  protection: ProtectionReport | null;
  protection_previous_status: 'ready' | 'needs_setup' | null;   // set while checking
  protection_last_known: ProtectionReport | null;
  rules_url: string;                            // https://github.com/<name>/settings/rules
  setup_url: string;                            // /settings/repository?repo_id=<id>
};

type SyncState = { status: 'never' } | { status: 'running'; started_at: number }
  | { status: 'complete'; finished_at: number } | { status: 'interrupted'; started_at: number };

type FactoryConfig = {                          // factory.json
  version: 1; sync_interval_seconds: number; access_max_age_seconds: number;   // 30..86400, max_age >= interval
  connections: Partial<Record<Role, { app_id: number; private_key_env: string } & ({ installation_id: number } | { installation_ids: number[] })>>;
  repositories: { id: number; name: string; enabled: boolean }[];
};

type RuntimeConfig = {                          // runtime.json, forge/factory/config.py DEFAULT
  version: 1; enabled: boolean; merge_enabled: boolean;
  slots: { build: number; review: number; audit: number; recovery: number };   // 0..64, recovery >= 1
  cycle_seconds: number; timeout_seconds: Record<'build'|'review'|'audit'|'recovery', number>;
  grace_seconds: number; availability_retry_seconds: number;
  image: string; engine: string; engine_socket: string; apparmor_profile: string | null; seccomp_profile_env: string | null;
  network: string; proxy: string; memory: string; cpus: number; pids_limit: number;
  harness_argv: string[]; provider_environment: string[]; human_reviewers: string[]; human_required: boolean;
  provider_auth_file_env: string | null; cycle_budget_seconds: number;
  provider: string; model: string | null;
  provider_pool: { id: string; provider: string; model: string | null; provider_environment: string[]; provider_auth_file_env: string | null }[];
  provider_ready: string[] | null;              // verified pool ids; null = all
  ready_label: string; hold_labels: string[]; merge_method: 'merge' | 'squash' | 'rebase';
};
```

### GET /api/factory
Producer: `forge.factory.control.view(root)` + `ok`, `controls_token`. Failure: `{ok:false, reason:"Factory state is unavailable; reconcile the durable ledger."}` (also when paused).

```ts
type FactoryView = {
  ok: true; controls_token: string;
  active: boolean;                              // state/factory-v2.enabled exists
  operator: OperatorStatus;
  repositories: RepositoryRow[];                // Settings(root).view().repositories
  observation: SupervisorObservation | {};      // state/factory/supervisor.json
  actions: OperatorRequest[];                   // last 10 operator requests
  issues: LedgerIssueView[];
  jobs: JobView[];                              // jobs where !cleanup || outcome === null
  slots: Record<string, string>;                // slot name -> job id
  runtime: RuntimeConfig;
  runtime_revision: string;                     // sha256 digest of runtime; send back on POST /api/runtime
};

type OperatorStatus = { title: string; detail: string; action_url: string | null; action_label: string | null; empty?: true };

type SupervisorObservation = {
  at: number;
  errors: { attempt?: string; issue?: string; phase: 'runner' | 'reconciliation' | 'github' | 'capacity' | string; reason: string }[];
  capacity: null | { allowed: boolean; reason?: 'memory_budget' | 'memory_headroom' | 'memory_pressure' | 'telemetry_unavailable' | string; [k: string]: unknown };
  issues: number;
};

type OperatorRequest = { id: string; action: 'stop' | 'reset' | 'retry'; target: string; reason: string; status: 'pending' | 'applied' | 'rejected'; error?: string };

type Candidate = { repo_id: number; issue: number; pr: number; head: string; base: string; base_ref: string; build_attempt: string; requirements: unknown; policy: unknown; key: string; accepted_at: number; branch?: string /* (unverified) */ };

type LedgerEvent = {
  sequence: number; at: number; issue: string;  // issue key "repo_id:number"
  kind: 'discovered' | 'assigned' | 'runner_released' | 'accepted' | 'failed_attempt' | 'investigated' | 'failure_corrected'
    | 'environment_blocked' | 'environment_recheck' | 'availability_wait' | 'candidate_invalidated' | 'held'
    | 'protection_paused' | 'protection_hold_migrated' | 'protection_restored' | 'cancelled' | 'human_retry'
    | 'operator_queue' | 'operator_backlog' | 'operator_pause' | 'operator_resume' | 'operator_retry' | 'operator_recheck' | 'operator_conflict'
    | 'merged' /* (unverified) */ | 'reserved' /* (unverified; issues.js maps it) */ | string;
  reason?: string | null; attempt?: string; role?: JobRole; slot?: string; provider?: string; model?: string | null; provider_id?: string | null;
  candidate?: Candidate; cause?: string; count?: number; findings?: unknown; blocker?: string; detail?: string; retry_after?: number; request?: string;
};

type IssueFailure = { attempt: string; role: JobRole; cause: string; investigation: 'pending' | 'complete' | string /* job id */; findings: null | { summary: string; pattern?: string; evidence?: string[]; uncertainty?: string; alternative?: null | { change: string; reason: string; evidence: string }; incomplete?: boolean }; retry_state: IssueState; at: number; charged: boolean; correction?: string };  // 'evidence' key is stripped from the view

type LedgerIssueView = {
  key: string; repo_id: number; repo: string; number: number; title: string;
  state: IssueState; reason: string | null;
  requirements: unknown;                        // (unverified) GitHub requirement snapshot captured at discovery
  candidate: Candidate | null;
  active: string | null;                        // active job id
  failures: IssueFailure[]; budget_start: number; failure_count: number;   // failure_count = charged failures since budget_start
  retry_plan: string | null;
  evidence: Partial<Record<'review' | 'audit', { outcome: string; summary: string; [k: string]: unknown }>>;
  created_at: number; last_observed?: number; retry_after?: number;
  operator_hold?: { kind: 'pause' | 'backlog' | 'conflict'; state: IssueState };
  operator_request?: string;                    // pending issue-action id
  prerequisite_hold?: { state: IssueState; legacy?: true };
  environment_hold?: { state: IssueState; blocker: 'toolchain' | 'sandbox' | 'dependency_access' | 'resources' | 'ci_evidence'; detail: string; attempt: string; environment: string };
  github_observation?: { at: number; issue: IssueFacts };
  history: LedgerEvent[];                       // last 50 events for this issue
};

type JobView = {
  id: string; issue_key: string; repo_id: number; repo: string; issue_number: number;
  role: JobRole; slot: string; provider: string; provider_id: string | null; model: string | null;
  publisher: string; branch: string; target_branch: string;
  runner_config: RuntimeConfig | null; candidate: Candidate | null; policy: unknown; requirements: unknown;
  created_at: number; deadline: number;
  phase: 'reserved' | 'created' | 'launching' | 'running' | 'stopping' | 'cleaned' | 'external' /* merge jobs (unverified) */;
  outcome: null | 'accepted' | 'failed' | 'investigated' | 'blocked_environment' | 'waiting' | 'superseded' | 'held' | 'cancelled' | 'external_merge';
  cleanup: boolean; fenced: boolean; cancelled?: boolean;
  capacity_wait_since?: number | null; last_observed?: number; container_id?: string; ci_evidence?: unknown; review_policy?: unknown; teardown?: unknown;
  observed_at: number | null;                   // added by view(): observation.at
  started_at: number | null;                    // added by view(): observation.execution.started_at (container start)
};
```

Operator status producer (`operator_status.overview`, evaluated in this order; first match wins):

| Condition | title | detail | action_url | action_label |
|---|---|---|---|---|
| `!active` | "Finish setting up Forge" | "Connect a repository and verify setup before starting." | `/onboarding` | "Continue setup" |
| `!runtime.enabled` | "Forge is paused" | "Issue processing is disabled. Existing worker cleanup continues." | `/settings` | "Review settings" |
| no `observation.at` or age outside `[0, max(120, cycle_seconds*3))` | "Waiting for the Forge service" | "The supervisor has not reported recently. Saved work remains in place." | `/setup-help#host-installation` | "Check service setup" |
| `observation.errors[0]` | "Forge needs attention" | `errors[0].reason` | `/settings` | "Review setup" |
| capacity denied for memory/telemetry | "Waiting for memory checks" (telemetry_unavailable) / "Waiting for available memory" | "Forge will retry automatically. Existing workers continue and queued issues keep their place." | null | null |
| an issue with `active` | "Building/Reviewing/Auditing/Merging/Investigating issue #N" (by job role; fallback "Working on") | issue title or repo | `https://github.com/<repo>/issues/<n>` | "View issue" |
| recovery job not cleaned | "Investigating issue #N" | "Forge is checking the failure before deciding the next attempt." | issue URL | "View issue" |
| waiting issue with `environment_hold` | "Forge’s build environment needs attention" | "The candidate pull request and code-retry budget are preserved." | `/issues?issue=<key>` | "Review environment blocker" |
| waiting issue with `prerequisite_hold` | "Prepare the repository to continue" | issue.reason | `/settings/repository?repo_id=<id>` | "Check repository" |
| waiting issue (approval_needed/hold/failed) | "An issue needs your attention" | reason or "Review the issue before continuing." | `/issues?issue=<key>` | "Review issue" |
| no enabled repos | "Choose where Forge should work" | "No repositories are enabled." | `/settings/github` | "Choose a repository" |
| repo missing access/protection | "Check repository setup" | "<name> needs a current access and review-settings check." | repo.setup_url | "Check repository" |
| current issues and no provider available | "Waiting for a coding account" | "A coding account is temporarily unavailable. Forge will retry when it can." | `/settings/harnesses` | "Review coding accounts" |
| queued issue whose next role has 0 slots | "Worker capacity is paused" | "The next stage has no worker slots enabled." | `/settings` | "Review worker capacity" |
| queued issue with `retry_after > now` | "Waiting before retrying" | reason or "An earlier availability check failed. Forge will retry automatically." | issue URL | "View issue" |
| any current issue | "Work is queued" | "The supervisor is checking eligibility and available worker capacity. Existing holds and ownership still apply." | `/settings` | "Review worker capacity" |
| `slots.build == 0` | "Builder capacity is paused" | "Enable a Builder slot to pick up ready issues." | `/settings` | "Review worker capacity" |
| some enabled repo never discovered | "Looking for ready issues" | "Forge is checking the enabled repositories. No work has been confirmed yet." | null | null |
| else | "Waiting for a ready issue" | "Choose an issue to queue for Forge. It will check eligibility before starting." | `/issues` | "Choose an issue" (+ `empty:true`) |

### GET /api/factory/output?id=<uuid>
Query must be exactly `id` (one value, canonical UUID). Returns `{ok:true, logs:string}` where logs = `(job.capture ?? job.observation ?? {}).logs ?? ''`. Failure: `{ok:false, reason:"Execution output unavailable"}`.

### POST /api/factory/action
Body exactly `{action:'stop'|'reset', target:string, reason:string}` (all three keys required, no others). `queue()` rules: `action==='retry'` → "Open the issue in Issues to review and retry its failed attempt."; action not stop/reset or blank reason → "Action and reason are required"; empty target → "Target identity is required". Shape mismatch → "Expected action, target, and reason". Reason truncated to 4096. Response `{ok:true, request:<uuid>}`. The request is durable; the supervisor applies it on its next cycle (status visible in `FactoryView.actions`).
- `stop`: target = job id. Cancels the issue's active attempt (or marks a recovery investigation "Investigation stopped by operator"), fences the job (`phase:'stopping'`).
- `reset`: target = job id (used from "Investigations and cleanup" on recovery/orphan jobs). Same code path: fence + stop.

### POST /api/runtime
Body exactly `{config: RuntimeConfig, revision: string}`. `config` is validated by `forge.factory.config.validate` (full object; unknown keys → "Unknown runtime settings"). Revision mismatch → "Runtime settings changed; reload before saving". Response = `FactoryView & {ok:true, controls_token}`. Max body 262144.

### GET /api/issues
Query params (each at most one value; any other key → invalid): `repo_id` (int), `state` (default `open`; one of `open|completed|all|backlog|queued|working|attention|waiting`, else "Choose a valid issue view"), `q` (substring, case-insensitive against `"<repo> #<number> <title>"`), `page` (int ≥ 1, 50 per page), `issue` (key `"<repo_id>:<number>"` → detail mode). Each GET also triggers a background inventory refresh (`start_refresh()`, throttled to once per 60 s per repo unless forced). Failure: `{ok:false, reason:"Issue inventory is unavailable. Refresh to try again."}`.

```ts
type IssueFacts = { number: number; title: string; body: string /* ≤16000 chars */; content_hash: string; body_truncated: boolean;
  state: 'open' | 'closed'; updated_at: string | null; labels: string[]; assignees: string[] };

type IssueScope = { id: number; name: string; complete: boolean; fresh: boolean; at: number | null; error: string | null };
// error text: "Could not finish refreshing. Previously received issues are still shown."

type IssueActionStatus = { id: string; key: string; status: 'pending' | 'applied' | 'rejected'; message: string | null };
// messages: "Decision applied. Forge will recheck eligibility before starting work." / "GitHub is unavailable. Forge will confirm this decision before starting work." /
// "Could not confirm the decision. Refresh and review the issue." / "Forge is resetting. No issue decision was applied." / any ValueError text from apply()

type IssueRow = IssueFacts & {
  key: string; repo: string; repo_id: number;
  status: 'backlog' | 'queued' | 'working' | 'attention' | 'waiting' | 'completed';
  explanation: string;                          // see presentation table
  actions: ('queue' | 'backlog' | 'pause' | 'resume' | 'retry' | 'recheck')[];
  setup_needed: boolean; environment_blocker: string | null;
  pull_requests: { number: number; title: string }[];
  dependencies: number[];                       // "blocked by / depends on / dependency:" #refs in body
  fresh: boolean;                               // false => actions are []
  revision: string;                             // optimistic-concurrency token for POST /api/issues/action
  setup_url: string;
  active: string | null;
};
type IssueDetail = IssueRow & {
  history: { at: number; kind: string; reason?: string | null }[];     // last 15 ledger events
  reviews: { role: 'review' | 'audit'; outcome: string | null; summary: string /* ≤1000 */ }[];
};
type IssueListResponse = { ok: true; controls_token: string; issues: Omit<IssueRow, 'body'>[] /* body, history, reviews stripped */; total: number; page: number; pages: number; repositories: IssueScope[]; complete: boolean; actions: IssueActionStatus[] };
type IssueDetailResponse = { ok: true; controls_token: string; issue: IssueDetail; repositories: IssueScope[]; actions: IssueActionStatus[] };
```
Detail error when key not found: "This issue is not in a managed repository. Refresh the issue list." List order: attention, working, waiting, queued, backlog, completed; then repo (lowercase), then number.

`presentation()` table (first match; `held` = issue labels ∩ `runtime.hold_labels`):

| Case | status | explanation | actions |
|---|---|---|---|
| GitHub closed or ledger merged | completed | "Completed and merged." / "Closed on GitHub." | [] |
| `operator_request` pending | waiting | "Applying your decision. Forge will confirm it before continuing." | [] |
| `active` | working | "Building" / "Reviewing" / "Security review" / "Merging" / "Investigating a failed attempt" / "Working" | [] |
| cleanup pending job | waiting | "Finishing cleanup from the previous attempt." | [] |
| held labels | attention | "Paused by <labels> on GitHub." (+ " Resolve the dependency before releasing this hold." unless exactly `hold`) | ['resume'] only if labels == {hold} |
| `environment_hold` | attention | "Forge’s build environment needs attention. The pull request and code-retry budget are preserved. Repair the environment, then recheck." | ['recheck'] |
| setup hold (`prerequisite_hold` or legacy reason) | attention | "Repository setup now passes. Recheck to resume." / "Repository setup needs attention." | ['recheck'] when repo protection+access ready |
| external PR / assignee / `wip` label | attention | "Existing work needs review before Forge starts another attempt." | [] |
| ledger cancelled | attention | "The previous attempt was stopped. Review before resuming." | ['resume'] |
| ledger failed | attention | issue.reason or "The last attempt failed." | ['retry'] |
| `operator_hold` | backlog "Not queued for Forge." ['queue'] (kind backlog) / attention "Paused by an operator." ['resume'] | | |
| ledger hold | attention | reason or "An operator decision is needed." | [] |
| approval_needed | attention | "The pull request needs a human review." | [] |
| built/reviewed/audited/investigating | queued | "Waiting for review." / "Waiting for security review." / "Checking merge requirements." / "Waiting for a failure investigation." | ['pause'] except investigating |
| ready label present | queued | "Queued for Forge." | ['backlog','pause'] |
| else | backlog | "Not queued for Forge." | ['queue'] |

Overrides: queued + `retry_after > now` → "Waiting for a coding account. Forge will retry automatically." (reason ∈ blocked_auth/blocked_quota/"Role credentials unavailable") else "Waiting before another check. Forge will retry automatically."; queued + `!runtime.enabled` → "Forge is paused. This issue stays queued."; `!fresh` → actions []; repo disabled or access not ready → actions [] and (unless completed/working) attention "Repository access or activation needs attention.".

### POST /api/issues/action
Body exactly `{key, action, revision, reason}` (all strings; `reason` non-blank, ≤ 2000) else "Expected issue, action, current revision, and decision" / "Choose an issue action and record the decision.". Server re-reads the issue: `revision` mismatch or action not in `row.actions` → "This issue changed or needs a fresh check. Review it again before continuing.". If a pending action for the same key exists with the same (action, revision) its id is returned; a different pending one → "Another decision for this issue is still being applied.". Response `{ok:true, request:<uuid>}`. Actions: `queue` (add ready label), `backlog` (remove ready label), `pause` (add `hold`), `resume` (add ready, remove `hold`), `retry` (new failure budget, back to Build), `recheck` (release prerequisite/environment hold). Applied by the supervisor; results appear in `actions[]` of subsequent GETs.

### POST /api/issues/refresh
Body exactly `{}` ("Expected an empty refresh request"). Forces a GitHub inventory refresh in the background. Response `{ok:true}`.

### GET /api/settings
Producer `settings_service().view()` (live `Settings(WD)` once `state/factory-v2.enabled` exists, else the onboarding `setup/` Settings). Failure: `{ok:false, reason:"Settings unavailable. Validate factory.json on the server."}`.

```ts
type SettingsView = { ok: true; controls_token: string;
  config: FactoryConfig; revision: string; configured: boolean; config_file: 'factory.json';
  roles: Record<Role, RoleAccess>; repositories: RepositoryRow[] /* sorted by name */; sync: SyncState };
```

### POST /api/settings
Body exactly `{config: FactoryConfig, revision}`. Validation errors from `validate()` (e.g. "Expected version 1 settings with connections, repositories, and sync limits", "Settings changed; reload before saving", "<name>: verify access for every role before enabling", "<name>: finish repository setup and check again before enabling", "Factory reset is in progress"). Response `SettingsView & {ok:true, controls_token}`. Max body 262144.

### POST /api/settings/sync
Body exactly `{}` ("Expected an empty sync request"). Starts background `refresh_all()` (access discovery for all four roles, then a protection check for enabled repos). Response `{ok:true, message:"Access refresh requested"}`. Progress is observed through `sync.status` on subsequent GETs.

### GET /api/onboarding
Producer `Onboarding(WD).view()` + `ok`, `controls_token`. Not blocked by the paused fence. Failure: `{ok:false, reason:"Setup state is unavailable; inspect the supervisor service"}`.

```ts
type SetupPhase = 'setup' | 'legacy' | 'active' | 'resetting';
type CheckStatus = 'checking' | 'passed' | 'failed';
type Harness = { id: string /* 'codex' | 'claude' */; name: string /* "Codex" | "Claude Code" */; version: string; connected: boolean;
  models: { id: string; model: string | null; status: 'connected' | 'passed' | 'failed' | 'unknown' | 'sign in again' | 'usage limit; retry pending' | 'verification required' }[];
  login_command: string; check_command: string };   // e.g. "/opt/forge/bin/forge setup login claude --root /srv/forge"
type SetupActivity = { target: 'setup' | 'github' | 'provider' | 'runner' | 'start' | 'reset'; label: string; provider?: string; model_id?: string | null };
// labels: "Waiting for available memory; verification will resume automatically" | "Checking <harness name>" | "Finishing <harness name>" | "Checking worker reuse" | "Checking worker cleanup" |
// "Checking repository access and protection" | "Waiting for active workers to finish before checking shared coding accounts; new work is paused" | "Preparing verification" | "Starting Forge" | "Resetting Forge" |
// "Waiting for workers using the <harness name> login to finish; other work continues" (scoped check)
type SetupOperation = { id: string; action: 'check' | 'harness_check' | 'start' | 'reset'; generation: string; revision: string | null;
  step: 'queued' | 'prepare' | 'probe' | 'stop_workers' | 'clear_setup' | string; at: number;
  harness?: string; providers?: string[]; image?: string | null;   // scoped `setup check <harness>` only
  [k: string]: unknown };

type OnboardingView = { ok: true; controls_token: string;
  phase: SetupPhase; generation: string; revision: string;
  account_type?: 'organization' | 'personal' | null; reset_receipt?: { id: string; generation: string } | null;
  roles: Record<Role, { status: RoleAccess['status']; registered: boolean; app_id?: number | null; install_url?: string | null }>;
  repositories: RepositoryRow[];                // only access_ready rows
  selected: { id: number; name: string } | null; owner: string; provider_connected: boolean;
  checks: Partial<Record<'github' | 'provider' | 'runner', CheckStatus>>;
  harnesses?: Harness[]; activity?: SetupActivity | null; repository_check?: ProtectionReport | null;
  repository_setup?: RepositoryRow | null;      // the selected repo's row
  qualification_fresh?: boolean; service?: { last_seen: number; available: boolean };
  can_start: boolean; operation: SetupOperation | null; error: string | null };
// phase 'resetting' returns the reduced shape: roles all {status:'not_configured', registered:false}, repositories [], selected null, owner '', provider_connected false, checks {}, can_start false, revision ''.
```

### POST /api/onboarding
Body `{action, ...fields}`; the field set must match exactly per action ("Invalid setup action"):

| action | fields | response |
|---|---|---|
| `github` | `{role: Role, owner: string, account_type: 'organization'|'personal'}` | `{ok:true, install_url}` if the role is already registered; else `{ok:true, action: "https://github.com/(organizations/<owner>/)?settings/apps/new?state=<token>", manifest: {...}}` — the page POSTs the manifest as a form field `manifest` to `action`. Errors: "Choose a role and a GitHub account name", "Choose a GitHub account type", "Use the same GitHub owner for all roles; reset to change owners", "Finish the current operation, or reset the existing factory before setting up again". |
| `refresh` | `{}` | `{ok:true, ...OnboardingView}` after re-discovering installations. |
| `repository` | `{repo_id: number}` | `{ok:true}`. Error "Refresh GitHub access and choose a repository available to all four Apps". |
| `provider` | `{api_key: string|null}` | `{ok:true}` (legacy Codex API-key import; the UI now uses terminal login and never sends this). |
| `check` | `{revision}` | `{ok:true, request: <op id>}`. Errors "Setup changed; reload before continuing", "Choose a repository and connect the coding provider first". |
| `start` | `{revision}` | `{ok:true, request}`. Extra error "Run readiness checks again before starting". Idempotent for an already-active generation. |
| `reset` | `{generation}` | `{ok:true, request}`. Writes `state/factory-paused.json`, phase → `resetting`. Error "This reset belongs to an earlier setup; reload before resetting the current factory". Returns the receipt id when the generation already reset. |

`harness_check` exists in `queue()` but no page sends it (terminal `bin/forge setup check` does). The API always queues a full
check: only the terminal's `bin/forge setup check <harness>` sets `harness`/`providers`, which verifies those pool entries, keeps the
other accounts' `provider_checks`/`provider_ready`, and leaves every harness outside the scope dispatchable while it runs.

### GET /onboarding/github/callback?state=&code=
Exactly those two params. On success **303** to `https://github.com/apps/<slug>/installations/new`; on any failure **303** to `/onboarding?connection_error=1` (page shows "GitHub connection was not verified. Reconnect from this page.").

### GET /api/repositories[?repo_id=<int>]
Producer `RepositorySetup(settings_service()).view(repo_id)`. Failure: `{ok:false, reason:"Repository setup changed or is unavailable. Check again."}` (includes unknown repo_id: "Refresh GitHub access to find this repository").

```ts
type RepositoriesView = { ok: true; controls_token: string; revision: string;
  repositories: RepositoryRow[];                // one row when repo_id given
  connections: Record<Role, RoleAccess & { install_url: string | null }>;
  sync: SyncState;
  activation_available: boolean;                // false during initial onboarding (setup/ settings)
  continue_url: '/onboarding' | '/factory' };
```

### GET /api/repositories/ruleset?repo_id=<int>&revision=<string>
Both required ("Select and check a repository first"). Returns the generated ruleset JSON (`Content-Type: application/json`, `Content-Disposition: attachment; filename="forge-review-rules-<id>.json"`), pretty-printed:
```json
{"name":"Forge review baseline","target":"branch","enforcement":"active","bypass_actors":[],
 "conditions":{"ref_name":{"include":["refs/heads/<branch>"],"exclude":[]}},
 "rules":[{"type":"pull_request","parameters":{"required_approving_review_count":2,"dismiss_stale_reviews_on_push":true,"require_code_owner_review":false,"require_last_push_approval":false,"required_review_thread_resolution":false}}]}
```
Errors (as `{ok:false, reason}`): "Repository settings changed. Check again before downloading.", "Check this repository before downloading its rules.", "Verify the repository branch before downloading rules".

### POST /api/repositories/check
Body exactly `{repo_id: number|null, revision}` (`null` = all enabled repos) else "Expected repository identity and current revision". Revision mismatch → "Repository settings changed. Reload and check again.". Starts a background protection check (rows go `protection_status:'checking'` with `protection_previous_status`). Response `{ok:true}`.

### POST /api/repositories/enable
Body exactly `{repo_id: number, revision}`. Errors: "Finish initial Forge setup before enabling automation", "Repository settings changed. Check again before enabling.", plus `Settings.save` errors. Response `RepositoriesView(repo_id) & {ok:true, controls_token}`.

### POST /api/slots/action
Legacy dashboard only (`{action, session, generation}` strings → `forge.slot_actions.request`). **Not used by any factory-era page**; omit from the Nuxt rebuild.

### Pages/assets served (for routing parity)
`/` (redirects 303 to `/onboarding` when not activated and onboarding enabled or no repos; else factory page when `factory-v2.enabled`, legacy dashboard otherwise), `/factory`, `/issues`, `/settings` (303 → `/onboarding` when not activated), `/settings/advanced` (same page as `/settings`), `/settings/github`, `/settings/repository`, `/settings/harnesses` + `/onboarding` + `/reset` (onboarding page, sets cookie), `/setup-help`, `/favicon.svg`, `/assets/issues.js`, `/assets/repository-setup.js`, `/assets/markdown.js`, `/assets/vendor/marked.umd.js`, `/assets/vendor/purify.min.js`.

---

## 3. Screens

### 3.1 Activity / Factory (`/` and `/factory`, `legacy/factory.html`)
- Title `Forge · Factory`. Eyebrow "LIVE OPERATIONS", h1 "Factory". Freshness pill (`#freshness`, role=status) initial text "Connecting…".
- Poll: `ForgeRefresh.poll` on `GET /api/factory`, default interval 5 s; `lastRead` recorded on each success. Local `tick()` every 1 s (skipped when hidden) to advance timers and freshness copy.
- Derived helpers (preserve):
  - `maxAge() = max(120, (runtime.cycle_seconds||30)*3)`.
  - `fresh() = observation.at && !connectionLost && -5 <= now-observation.at < maxAge()`.
  - `jobFor(issue) = jobs.find(j => j.id===issue.active && !j.cleanup && !j.outcome)`.
  - `running(job) = job.phase==='running' && !cleanup && !fenced && !outcome && fresh() && (observed_at||last_observed) && now-(observed_at||last_observed) < maxAge()`.
  - `eligible(issue)` = repo not in `repositories` or `repo.enabled`. `queued(issue) = !jobFor(i) && nextRole[i.state] && eligible(i)` with `nextRole = {ready:'build', built:'review', reviewed:'audit'}`.
  - `elapsed(start,end)`: `<60` → "N sec", `<3600` → "N min", else "Nh Mm". Timer element uses `job.started_at`; end = now when running, else `observed_at||last_observed||observation.at` (frozen when stale/offline). `title="Time since this worker started"`.
  - `latestProvider(issue)` = provider of the last `availability_wait` history event.
  - `reason(issue, job)`: job `capacity_wait_since` → "Waiting for memory. Forge will start this worker automatically when space is available."; job present and reason in blocked_auth/blocked_rate_limit → ""; reason `blocked_auth` → "<Claude|Codex|A coding account> needs sign-in. Queued for another available coding account."; `blocked_rate_limit` → "Coding account usage limit reached. Forge will retry automatically."; else `issue.reason||''`.
  - `phase(issue, job)`: with job: capacity wait → "Waiting for memory"; running → "Working"; phase running but stale → "Last seen working"; reserved/created/launching → "Starting"; stopping → "Finishing"; external → "Merging"; other → "Finishing". Without job, by state: ready/built/reviewed "Queued", audited "Ready to merge", approval_needed "Approval needed", investigating "Investigating", hold "On hold", failed "Needs attention", merged "Merged", cancelled "Cancelled", else "Waiting".
- Overview block: `#operator-title` = `operator.title` || (runtime.enabled ? "Factory is running" : "Forge is paused"); initial "Checking factory status". `#status` = `operator.detail` || "Work moves through Build, Review and Security audit before merge."; initial "The first observation will appear here.". `#next-action` = link `<action_label> →` to `action_url` when present.
- Notice line (`#notice`, amber): connection lost → "Connection interrupted. Showing the last known state; retrying automatically."; else last rejected operator request → "Action could not be applied: <error>"; else `observation.errors[0].reason` or empty. First-load failure: freshness "Unable to connect", notice "No factory data received yet. Retrying automatically.".
- Freshness: connectionLost → "Reconnecting · last known state"; fresh → "Live · checked <elapsed> ago"; else "Updates delayed · last known state" (class `stale`, amber; `main.is-stale` pauses animations and dims timers).
- Metrics (6 tiles, `metrics()`): running jobs = phase running, not cleanup/fenced/outcome, with observed time within maxAge of `observation.at`.
  - "Queued" = count of `queued(i)`; detail "Waiting for a worker" or "Dispatch paused" when `!runtime.enabled`.
  - "Builders"/"Reviewers"/"Auditors" (data-role build/review/audit): value = running jobs of that role; detail = per-job links (spinner + timer, or "Last seen <timer>") anchored to `#issue-<repo>-<n>` or `#stage-<role>`, aria-label "<Workers> on issue N"; when 0 → "<slots[role]> maximum". Recovery jobs are never counted.
  - "Needs attention" = issues in hold/failed/approval_needed, detail "Awaiting a decision". "Merged" = merged issues, detail "All tracked issues".
- Columns (`#columns`): three sections `#stage-build`, `#stage-review`, `#stage-audit` titled "Build", "Review", "Security audit" with role illustrations. Membership: issue whose job role matches, else `nextRole[state]===role`. Summary line "<working> working · <waiting> queued[ · <starting> starting / finishing | last observed] · <slots[role]> maximum". Empty copy "No issues at this stage.".
- Card (`article.issue-card`, `data-key=<issue key>`, `id=issue-<repo_id>-<number>`, class `is-working` when running): meta "<repo> · #<n>" + state label `phase()`; h3 title (fallback "Issue #n"); progress line: spinner when running, then provider name ("Claude Code" / "Codex" / role) + " · <model>" + timer, or "Waiting for a worker" when queued; reason paragraph; links "Issue #n ↗", "PR #p ↗" (candidate.pr), "Prepare repository →" (`/settings/repository?repo_id=` when `prerequisite_hold`), "Coding accounts" (`/settings/harnesses` when reason blocked_auth and no job); "<failure_count> / 3 attempts failed"; footer "Review issue →" (`/issues?issue=<key>`, `data-issue-view`) and for non-merge jobs buttons "Output" and "Stop".
- Support panels: "Needs attention · <n>" (issues failed/hold/approval_needed not already in a column; empty "No issues need your attention."), "Finishing" note "Approved work waiting to merge, or being merged." (state audited or merge job; empty "No issues waiting to merge."), details "Investigations and cleanup · <n>" note "Internal recovery is shown separately from productive workers." listing issues in `investigating` plus internal jobs (recovery or not referenced by any issue.active) as "Investigation · <phase>" / "Worker cleanup · <phase>" with "Output" and "Reset" buttons; details "Completed issues" (merged/cancelled; empty "No completed issues yet.").
- Groups assignment order: columns → attention → finishing → runners → completed; an issue appears once.
- Dialog (`<dialog>`): "Output" → `GET /api/factory/output?id=` → title "Worker output", body logs or "No captured output is available yet."; "Stop" → title "Stop this attempt?", text "This cancels the issue’s active attempt, discards unfinished workspace edits, and resets its runner. Published PRs are preserved.", confirm "Stop attempt"; "Reset" → "Reset runner?", "This terminates the old execution and discards its disposable workspace. Capacity is released only after teardown is verified.", confirm "Reset runner"; (retry, unreachable) "Retry issue?", "This grants a new three-failure budget and sends the issue to Build. Existing history is preserved. Explain what has changed.", confirm "Retry issue". Reason input label "Decision or reason" (required, trimmed). Buttons "Close" / "Confirm". Submit → `POST /api/factory/action {action,target,reason}`; on success close, refresh, notice "Action queued for the supervisor."; on error show message in the dialog explanation.
- Transitions: cards are keyed nodes; moving between columns animates (FLIP, 420 ms, skipped under reduced motion) and announces "Issue <n> moved to <column h2>" in an aria-live region. Focus is preserved across renders.
- Colors: build `#94bfff`, review `#c9a4f4`, audit `#80d7bd`, merge/controller `#edbd77`; stale/amber `#edbd77`; background `#101416`; card `#1e252a`.

### 3.2 Issues list (`/issues`, `legacy/issues.html` + `issues.js`)
- Title `Forge · Issues`. Eyebrow "Your work", h1 "Issues", intro "Choose what Forge should work on. Review anything that needs a decision.". Refresh icon button aria-label "Refresh issues".
- Filters: search "Find an issue" placeholder "Title, repository or issue number" (200 ms debounce, resets page to 1); select "Repository" ("All repositories" + scopes); select "Show" options: open "Open issues", attention "Needs attention", backlog "Backlog", queued "Queued", working "Working", waiting "Waiting", completed "Completed".
- Poll: interval 5 s, `allowed: !busy && !decision`; each load fetches the list (`/api/issues?<params>`) and, when an issue is selected, `/api/issues?issue=<key>` in parallel; results are discarded if the params/selection changed meanwhile. Failure writes `error.message` to `#detail-message` (when a detail is open) or `#freshness`; recovery clears `#detail-message`.
- Row: "<repo> · #<n>", title link (`/issues?issue=<key>`), "Pull request #<first pr>" when any; status dot with `names[status]` = backlog "Backlog", queued "Queued", working "Working", attention "Needs attention", completed "Completed", waiting "Waiting"; explanation; action pill `rowAction`: pending action → "Applying…", working → "View progress", attention → "Review", else "View issue". Status colors: attention amber `#ecc486`, working/completed green `#bfe6cd`, queued blue `#abcfff`.
- Empty states: no repositories → "Choose where Forge should work" / "Add and enable a repository to see its issues here." + link "Add repository →" (`/settings/repository`); `!complete` → "Loading the issue list" / "Previously received issues remain visible while Forge checks GitHub."; else "No issues in this view" / "Try another filter.".
- Freshness line: initial "Loading issues…"; a scope with `error` → that error (class `warning`); no repositories → ""; complete → "Up to date · Viewing issues never queues them for work."; else "Showing received issues. The GitHub inventory is still incomplete or needs refreshing.".
- Count: "<total> issue|issues[ received] · Page <page> of <pages>"; "Previous"/"Next" buttons disabled at bounds.
- Refresh button → `POST /api/issues/refresh {}` then reload; errors shown in freshness.

### 3.3 Issue detail dialog (side panel `<dialog id="detail">`)
- Opened by any `[data-open]` click; URL becomes `/issues?issue=<key>` via `history.replaceState`; loading title "Loading issue…"; close button aria-label "Close issue" ("×"); Escape closes and URL returns to `/issues`.
- Renders: meta "<repo> · #<n>", title, status label + dot, explanation (+ " " + `environment_blocker`), reviews "Review: passed|needs attention · <summary>" / "Security review: …", description = `ForgeMarkdown.render(body || 'No description has been added yet.', repo)` plus "Description shortened. Open on GitHub to read the full issue." when `body_truncated`; links "Open on GitHub ↗" and "Pull request #<n> ↗"; dependencies "Depends on #a, #b" (each opens that issue `repo_id:n`).
- Markdown rules (`legacy/markdown.js`): marked GFM → DOMPurify with allowed tags p br hr h1–h6 strong em del s a code pre blockquote ul ol li table thead tbody tr th td details summary input; attrs href title start align type checked disabled open; non-checkbox inputs removed, checkboxes disabled; links resolved against `https://github.com/<repo>/blob/HEAD/`, only http/https/mailto kept, `target=_blank rel=noopener noreferrer`.
- Actions: buttons for `detail.actions` with `labels` = queue "Queue for Forge", backlog "Return to backlog", pause "Pause", resume "Release hold", retry "Retry attempt", recheck "Recheck readiness", stop "Stop attempt"; first button is `.primary` when it is queue/resume/retry/recheck, others `.secondary-pill`; disabled while busy or a pending action exists for the key. Extra: "Repository setup →" link to `setup_url` when `setup_needed`; "Stop attempt" when `active && status==='working'`.
- Decision panel: title `labels[action] + '?'`, effect text `effects`: queue "Forge will check repository setup, dependencies and existing work before starting when capacity is available."; backlog "This removes the issue from Forge’s queue. The GitHub issue stays open."; pause "Forge will leave this issue on hold until you choose to resume it."; resume "This releases the deliberate hold. Confirm that the prerequisite work is complete. Forge will check dependencies and existing work before continuing."; retry "This grants a new three-attempt budget and returns the issue to Build. Existing history is preserved."; recheck "Forge will recheck readiness and resume the held stage. The existing pull request and failure budget are preserved. Confirm that the reported blocker has been addressed."; stop "This stops the active attempt and discards its unfinished workspace changes. Published pull requests are preserved.". Reason textarea labelled "What changed?" is required for pause/resume/retry/stop; otherwise reason = `"<label> confirmed by operator"`. Buttons "Cancel" (reloads) and confirm labelled with the action label.
- Submit: stop → `POST /api/factory/action {action:'stop', target: detail.active, reason}`; others → `POST /api/issues/action {key, action, revision, reason}`. Success message "Decision submitted. Waiting for confirmation."; errors replace `#detail-message`. Then `#detail-message` follows the last action for the key: pending → "Applying your decision…", else its `message`. Stale detail without action → "Showing the last received issue. Refresh before making a decision.".
- "Recent activity" details list: `<time>` local string + event label from `events` map: discovered "Added to Forge", held "Put on hold", protection_paused "Repository setup paused work", protection_hold_migrated "Recognized an earlier setup hold", protection_restored "Repository setup verified", human_retry "Another attempt approved", operator_queue "Queued for Forge", operator_backlog "Returned to backlog", operator_pause "Paused by an operator", operator_resume "Hold released", operator_recheck "Repository setup checked", operator_conflict "Decision needs another review", reserved "Attempt prepared", merged "Pull request merged", cancelled "Attempt stopped"; other kinds shown with underscores replaced by spaces; reason shown beneath. Empty: "No Forge activity yet.".

### 3.4 Settings · General (`/settings`, `/settings/advanced`; `legacy/settings.html`)
- Title `Forge · Settings`; eyebrow "Settings", h1 "General", description "Manage worker capacity and factory setup.". "Continue setup" link (`/onboarding`) shown when `onboarding.phase !== 'active'`.
- Poll: `Promise.all([GET /api/settings, GET /api/onboarding])` every 5 s, `allowed: !busy && !dirty`. Failure notice "Could not refresh. Showing the last received data. <message>" (or just the message with no prior data), class `error`; recovery clears it. Non-error notices auto-clear after 6 s.
- After the first poll (General only) `GET /api/factory` once to load `runtime` and `runtime_revision`.
- "Worker capacity" section: intro "Set the maximum workers for each role. Forge automatically starts only as many as available memory safely allows."; inputs "Builders", "Reviewers", "Security auditors" (number 0–64, bound to `runtime.slots.build/review/audit`); note "These are upper limits, not reserved workers. When memory is tight, work stays queued and resumes automatically. Changes apply as workers become available."; when dirty show "Save worker limits" (primary) and "Discard changes". Save → `POST /api/runtime {config: {...runtime, slots:{...}}, revision: runtime_revision}` (full runtime echoed back unchanged) → message "Worker limits saved. They apply as workers become available."; discard reloads `/api/factory` and shows "Changes discarded.". Errors (e.g. "Runtime settings changed; reload before saving") shown in the notice; inputs keep the edited values.
- Danger zone: label "Destructive action", h2 "Reset Forge", "Stops all Forge workers, discards unfinished local work, and clears this factory’s setup and saved connections. You will need to set up Forge again." / "Your GitHub issues, branches, pull requests and App registrations are preserved." Link "Review reset…" → `/reset`.
- GitHub connections and Repositories sections are hidden on General.

### 3.5 Settings · GitHub access (`/settings/github`)
- Same page; title `Forge · GitHub access`, h1 "GitHub access", description "See the four Forge Apps, their verified permissions, and repository access. Refresh after changing access on GitHub.". Worker capacity and reset sections hidden.
- "GitHub connections" with icon button aria-label "Refresh GitHub access" (disabled until data). Status line: "Loading settings…" → "Last refresh <locale datetime>" (from `sync.finished_at` or max roles `observed_at`) or "Access has not been refreshed yet." + " · Refresh interrupted; the previous data is shown." when `sync.status==='interrupted'`. Help callout "Help · repository access" / "To add repositories, use **Manage access** for each App on GitHub, then **Refresh** here. Forge manages the App credentials."
- Connection card per role (build/review/audit/controller): `labels` Builder/Reviewer/Auditor/Controller; `purposes` "Writes code and opens pull requests." / "Reviews proposed changes." / "Checks changes for security issues." / "Coordinates issues and merges approved pull requests."; identity = `roles[role].app_slug` or slug parsed from onboarding `install_url`, fallback "No App connected"; badge `statusNames`: ready "Access verified", ok "Connected", unknown "Access unknown", not_configured "Not connected", stale "Refresh needed", missing_permissions "Permissions missing", missing_access "No access", name_changed "Repository renamed", archived "Archived or disabled"; if no `config.connections[role].app_id` but onboarding says registered → "Installation unverified". Observation: status ok → "<n> repository|repositories with verified access"; else `observed_at` → "Last checked <locale>"; else "Access has not been verified.". Action link: canonical `installation_url` → "Manage access ↗"; else no app_id and canonical `install_url` → "Install App ↗"; else no app_id → "Connect in setup →" (`/onboarding`). Errors: `reason` and "Missing <permissions>".
- Refresh: dirty → error "Save or discard your edits before refreshing access."; `onboarding.phase==='setup'` → `POST /api/onboarding {action:'refresh'}` then message "Access refreshed."; else `POST /api/settings/sync {}`. Button spins while `sync.status==='running'`.
- "Repositories" section: count "<n> enabled | Setup in progress · <total> discovered or configured"; link "Add repository" (`/settings/repository`); icon button "Refresh repositories" (→ `POST /api/repositories/check {repo_id:null, revision}`; disabled when no enabled repo; hidden while setting up); intro "Choose Enable to check a repository and finish its setup. Automation starts only after you confirm."; search "Find a repository" placeholder "Repository name" (client-side filter); table columns "Repository" / "Automation"; empty "Connect the Forge Apps through setup, then refresh access to discover repositories."; footer "Forge checks existing issues and pull requests before starting work. If access is lost, new work pauses and issue history is kept.".
- Row: state icon (aria-label "Needs attention" amber when enabled but access or protection not ready [protection counts as ready while `checking` with `protection_previous_status==='ready'`], "Enabled" green, "Not enabled"), name link to `setup_url`, "Needs attention" note; action: setting up → "Continue setup →"; enabled → optional "Resolve →" + quiet button "Disable"; else pill "Set up" (`/settings/repository?repo_id=`). Disable → `POST /api/settings {config with enabled:false, revision}` → "Settings saved."; dirty guard "Save or discard your edits before changing repository activation.". Enabling never happens from this table.

### 3.6 Settings · Repository setup (`/settings/repository[?repo_id=]`, `legacy/repository-setup.html` + `.js`)
- Title `Forge · Prepare repository`; breadcrumb "Settings › Repo setup"; progress steps "Repository" › "App access" › "Review rules" › "Enable" (last reads "Finish setup" when `!activation_available`). Stage = 0 no repo, 1 `!access_ready`, 2 protection not ready, 3 ready (setup mode), 4 enabled and live.
- Poll: `GET /api/repositories[?repo_id]` every 3 s, `allowed: !busy`; rejects when `!ok`, repositories not an array, or (repo_id given) empty → "Repository setup is unavailable. Return to GitHub access and refresh.". Failure: `#error` = "Could not refresh. Showing the last received data. " + message (prefix only when data exists); recovery clears that prefix message. Auto-check: when `access_ready` and `protection_status` ∈ unchecked/stale and key `revision:protection_status:observed_at` not yet checked → `check()`.
- Picker (no repo_id): h1 "Add repository", "Choose a repository. Forge will guide you through the setup.", panel "Choose your repository", search "Find by name" placeholder "owner/repository", list of non-enabled repos linking to `?repo_id=`, "No matching repositories yet.", details "Repository missing?" → "Give a Forge App access to the repository on GitHub using the links below, then refresh access. It will appear here once Forge can see it.".
- App access panel ("Connect the Forge Apps", refresh icon "Refresh GitHub access"): description "Give each App access to <name>. After making changes on GitHub, refresh access here." or "Use Manage access to add your repository to a Forge App, then refresh access here."; per role Builder/Reviewer/Auditor/Controller: ready → "Connected ✓"; else message `accessLabels`: missing_access "Needs repository access", not_configured "Not connected", missing_permissions "Permissions need updating", unknown/stale "Refresh to check access", archived "Repository is archived or disabled", name_changed "Repository was renamed", default "Choose the repository in GitHub’s access settings."; link "Manage access ↗" (canonical installation_url) / "Install App ↗" (canonical install_url) / "Connect App →" (`/onboarding`). Refresh → status "Checking App access…", `POST /api/settings/sync {}` when `activation_available` else `POST /api/onboarding {action:'refresh'}`; completes when `sync.status==='complete'` with a new `finished_at` → "Access refreshed."; `interrupted` or >90 s → "The access check did not finish. Refresh access to try again."; error text "Could not refresh access. Try again.".
- Repository summary: name, "Change" link, "Default branch · <branch>" or "Repository review settings"; status pill `labels`: ready "Ready to enable", needs_setup "Setup needed", unknown "Check unavailable", unchecked "Checking setup…", stale "Check setup again", checking "Checking setup…", access_needed "App access needed"; when ready: live+enabled "Enabled", live "Ready to enable", setup "Ready to continue"; " · Refreshing…" appended while checking with a previous status. h1: live → "Repository setup" (enabled) / "Enable repository", setup → "Prepare your repository". Description: ready+canEnable "Setup is complete. Enable automation to let Forge pick up eligible issues in this repository."; ready+live "Setup is complete and automation is enabled. No action is needed here."; ready+setup "Repository setup is complete. Continue to finish setting up Forge."; needs_setup "Update the GitHub review rules below, then check again."; access_needed "Connect the missing Apps before continuing."; else "Forge checks App access and GitHub review rules before enabling automation.".
- Requirements list (failed ones, excluding id `readable`) with "✓"/"○" and label/detail. Steps (hidden when ready or not prepared): 1 "Download the review rules" / "A ready-to-import file. No editing or administrator token needed." link "Download ruleset" (→ `/api/repositories/ruleset?repo_id&revision`, `download="forge-review-rules-<id>.json"`; becomes "Download again"; hidden when `edit_url` exists; no href unless status ready/needs_setup and not busy); 2 "Import on GitHub" / "On GitHub, choose New ruleset → Import a ruleset. Select the file, review the settings and save." link "Open GitHub settings ↗" (`rules_url`) — or with `edit_url`: "Update the existing ruleset" / "A Forge review ruleset already exists. Update its review settings using the requirements above." link "Edit ruleset on GitHub ↗"; scope "Applies to every pull request targeting <branch|the default branch>. Existing organization rules and stronger protections still apply."; 3 "Return here to check" / "After saving on GitHub, check that the review requirements are in place.".
- Hint: access_needed "Finish connecting the Apps above to continue."; stale "Check again to confirm the current GitHub settings before continuing."; unknown → first error detail or "GitHub could not be reached. Try checking again." + " Showing the last checked requirements." when a last-known report exists; checking "This usually takes a few seconds. Automation stays unchanged.".
- Footer: icon button "Refresh repository" (→ `POST /api/repositories/check {repo_id, revision}`; error default "Could not start the check."), "Last checked <HH:MM>", primary "Enable automation" (→ "Enabling…"; only when live, not enabled, ready; `POST /api/repositories/enable {repo_id, revision}` → response replaces snapshot, focus moves to status; error default "Could not enable this repository."), pill "Continue to Forge →" (setup mode, ready; href `continue_url`).
- Details "Setup details": all requirements, "Forge reads review requirements using its existing GitHub access. It cannot change your rules.", "**Bypass permissions need your review.** GitHub may hide who can bypass a ruleset. Check the bypass list on GitHub; a successful check here does not certify that list.", other rules "GitHub also enforces <types>." or "GitHub applies any additional repository protections at merge time.", sources list "<name>[ · ruleset] ↗", link "Read the setup guide →" (`/setup-help#github-repository-protection`).

### 3.7 Settings · Coding accounts (`/settings/harnesses`, rendered by `legacy/onboarding.html`)
- Header "Settings" / "Coding accounts" / "Manage sign-in from the Forge VM terminal.". Sets the `forge_setup` cookie like `/onboarding`.
- Data: `GET /api/onboarding` (`harnesses`, `activity`, `checks`, `error`). Polls every 3 s on this path (see 4).
- Panel row per harness: name; sub-line when connected: each model "<model|Default model>[ · Verified|Verification failed|Needs verification]" (suffix only when not fully verified) or "Needs verification"; not connected "Not connected". Right side: activity spinner + label when `activity.target==='provider' && activity.provider===id`; "✓ Verified" when connected and every model `passed`; else primary button "Verify connection" (connected) / "Connect". Footer "Connected harnesses and models take turns across all roles. If one is unavailable, Forge uses another.".
- Dialog: title "Verify <name>" / "Connect <name>"; sign-in block (hidden when connected) "Run this command in a terminal on the Forge VM, as the Forge service account. Follow the harness’s sign-in instructions." + `login_command` + "Copy sign-in command"; "After signing in, verify the connection:" (or when connected "Run this command in a terminal on the Forge VM, as the Forge service account, to verify the connection:") + `check_command` + "Copy verification command"; "Account status updates automatically after verification."; "Close". Copy buttons become "Copied" or "Select and copy the command above".
- Error notice shown only when `checks.provider==='failed'` or `checks.runner==='failed'`.

### 3.8 Onboarding (`/onboarding`, `/reset`; `legacy/onboarding.html`)
- Layout: left rail steps "1 GitHub", "2 Repository", "3 Start" (step 2 disabled until all roles `ok`, step 3 until `selected`); solo layout on `/reset`, `/settings/harnesses`, resetting, legacy, active. Footer "Setup help" (`/setup-help`) · "Reset Forge" (`/reset`, hidden while resetting).
- Boot: `GET /api/onboarding`; initial step = selected ? 2 : all roles ok ? 1 : 0. `?connection_error` → notice "GitHub connection was not verified. Reconnect from this page.". If phase setup, no selection, some role registered and no operation → auto `refresh`.
- Re-render only when the JSON signature changes (signature drops `service.last_seen`, adds a derived `waitingService` flag).
- Phase screens: `legacy` → "Fresh setup" / "Start with a clean factory" / "Your existing factory is still running. Reset it before creating the new setup." links "Reset existing factory" (`/reset`), "Open dashboard" (`/`); `active` → "Setup complete" / "Forge is enabled" / "Open the factory to see the service status and the next eligible issue." link "Open factory" (`/factory`); `resetting` → "Reset in progress" / "Resetting Forge" / "New work is blocked while Forge stops and cleans up its resources." with spinner text by `operation.step`: queued "Stopping scheduling", stop_workers "Stopping workers and verifying cleanup", clear_setup "Archiving history and clearing setup", else "Verifying reset"; note "Reset continues if you close this page. Fresh setup becomes available after cleanup is verified.".
- Step 1 GitHub: "1 · GitHub" / "Connect your GitHub workspace" / "You own the Apps and choose which repositories they can access."; role rows Builder/Reviewer/Auditor/Controller with "Connected" / "App created · install it on GitHub" / "Create a distinct GitHub App"; inputs "GitHub account or organization" (placeholder "your-organization") and "Account type" (Organization / Personal account) until owner known, then "GitHub owner: <owner>"; footer button "Connect <Role>" / "Install <Role>" / "Continue" with note "GitHub will ask you to approve each App and its installation." / "All four identities are verified."; text button "Refresh access". Connect → `POST {action:'github', role, owner, account_type}`: `install_url` → navigate; else build a hidden form POST with field `manifest` to `action`. Install → navigate to `roles[role].install_url`.
- Step 2 Repository: "2 · Repository" / "Where should Forge work?" / "Choose one repository to start with. You can add more later."; radio rows "<name>" / "All four roles have access"; empty "No shared repositories yet. Install all four Apps on the same repository, then refresh access."; "Refresh access"; footer "Continue" (note "Selecting a repository does not start work.") → `POST {action:'repository', repo_id}`; no selection → "Choose a repository to continue".
- Step 3 Start: "3 · Start"; title/intro by state — waitingService (operation, service unavailable, >15 s): "Waiting for the Forge service" / "Your setup is saved. The service has not picked up this request yet. Check that the Forge supervisor is running on the server." + link "Check service setup →" (`/setup-help#host-installation`); checking: "Starting Forge" (start) or "Verifying setup" / "Forge is checking access, coding accounts and worker cleanup. This does not start issue processing." + activity or "Verification in progress… You can leave and return later."; ready (`can_start`): "Ready to start Forge" / "Verification passed. You decide when issue processing begins." + button "Start Forge" (help "Starts processing eligible ready issues. Forge creates PRs and merges them when required checks and approvals pass."); needsRules (`repository_setup && !protection_ready`): "Prepare your repository" / "Check the review requirements and download a ready-to-import ruleset if anything is missing." + pill "Prepare repository →" (help "You apply the rules on GitHub. Forge only reads them."); not connected: "Connect a coding account" / "Connect at least one coding account before verifying this repository." + pill "Connect a coding account" (`/settings/harnesses`); failed (`error`): "Setup needs attention" / "Update the repository settings listed below, then check again. Your coding accounts do not need reconnecting." (when github check failed with a report) or "Verification did not finish. Resolve the error above, then check again." + button "Check again"; expired (github passed, not ready): "Verify your repository" / "The previous verification has expired or setup changed. Verify again before starting."; default "Verify your repository" / "Your coding accounts are connected. Repository verification is still required before starting." + button "Verify repository" (help "Rechecks repository access and protection using your recent account and worker verification." when `qualification_fresh` else "Verifies accounts and worker isolation. This includes small model requests that may incur provider charges."). Check/Start → `POST {action, revision}`.
- Step 3 extras: selected repo panel "<name>" / "Eligible issues labeled ready · Build → Review → Audit → Merge" + button "Change repository"; repository problem panel (github failed with `repository_check`): "<n> repository setting(s) need(s) attention" or "Repository protection could not be verified", "Branch: <branch>", link "Prepare repository →", failed requirements, details "<n> requirement(s) already pass(es)", link "Repository protection instructions" (`/setup-help#github-repository-protection`); "Coding accounts" summary with "Manage accounts" link and per-harness "Verified" / "Needs verification" / "Not connected"; runner activity row; details "Technical checks[ — coding accounts and workers passed]" rows "Repository permissions and protection", "Coding account connections and isolation", "Worker stop, cleanup and reuse" with value = activity label, or check status (`checking` → "Waiting" when an operation exists else "Incomplete"; missing → "Not run in this attempt" when github failed else "Not checked"); details "How Forge will work": "Build → Review → Security audit → Merge. Human approval is required when repository policy or the auditor calls for it." / "Three failed attempts stop an issue for a human decision. Each failure is investigated.".
- Failed-request links: "Check GitHub access" (`/settings/github`) · "Repository protection instructions".
- After `start`, polling continues; when `phase` becomes `active` the page navigates to `/factory`.
- Reset (`/reset`): "Reset Forge" / "Start fresh" / "Stop this factory and return to a blank setup."; list 01 "Stop all Forge-managed work" / "Pause scheduling and terminate every owned worker.", 02 "Discard unfinished local work" / "Remove disposable workspaces and pending factory work. Existing legacy checkouts are preserved.", 03 "Clear the current setup" / "Remove selections, settings and Forge-owned credential copies.", 04 "Keep your GitHub work" / "Issues, commits, branches, PRs and App registrations remain."; note "Previous lifecycle history is archived. Shared credentials and unrelated host processes remain untouched."; "Cancel" (`/`) and danger button "Stop and reset Forge" → `POST {action:'reset', generation}`. Completion (phase setup, receipt present and blank state, or generation changed): "Reset complete" / "Forge has been reset" / "Cleanup is verified. Your GitHub work has been preserved." + "Old GitHub Apps remain registered. You can remove them on GitHub separately." + link "Set up Forge →" (`/onboarding`). A reset error (`error`) is shown in the notice and the completion screen is withheld.
- Notice: `actionError` (last request failure) wins; else `state.error` unless it is the repository-check failure already explained inline on step 3.

### 3.9 Help (`/setup-help`, `forge/setup_help.py`)
- Title `Forge · Setup help`. Renders `docs/onboarding.md` (installation copy at `$FORGE_ROOT/docs/onboarding.md` overrides the repo file) with a restricted Markdown subset: `#`/`##`/`###` headings (ids = slugified text), paragraphs, `-` and numbered lists, 4-space code blocks, inline `code` and `[label](url)`; relative `*.md` links rewrite to `https://github.com/XRPL-Commons/forge/blob/main/docs/<file>`; non-http(s) links are rendered as text. No raw HTML.
- Layout: sticky sidebar "On this page" with links to h1/h2 anchors; main with eyebrow "Help". Current headings/anchors: "Set up Forge" (`#set-up-forge`), "Maintain GitHub access" (`#maintain-github-access`), "GitHub repository protection" (`#github-repository-protection`), "Host installation" (`#host-installation`), "First work" (`#first-work`), "Reset and restart" (`#reset-and-restart`), "Configuration and recovery" (`#configuration-and-recovery`). Deep links used elsewhere: `#host-installation`, `#github-repository-protection`.

---

## 4. Polling rules

### `ForgeRefresh.poll` (`forge/polling.py`, used by factory, issues, settings, repository setup)
- Options `{load, changed, failed, recovered, interval=5000, allowed=()=>true}`; returns `{refresh, start}`.
- `refresh()` coalesces: a call while a load is pending returns the same promise. When `document.hidden` or `!allowed()` it schedules the next tick and resolves with the last value without loading.
- On success: `changed(next)` only when `JSON.stringify(next)` differs from the previous signature; `recovered()` when the previous attempt failed; schedule `interval`.
- On failure: `failed(error, previousValue)`; schedule `max(interval, 15000)` (backoff floor 15 s). Previous data is retained.
- Scheduling is skipped entirely while hidden; `visibilitychange` to visible cancels the timer and refreshes immediately.
- `ForgeRefresh.html(parent, markup)` patches DOM in place (keyed by `data-key`), preserving focus/selection/open details. `ForgeRefresh.button(el, working, disabled)` toggles spinner state ("Refreshing…"/"Refresh" for text buttons, `forge-refresh-active` + `aria-busy` for icon buttons).
- Page intervals: factory 5 s (+1 s local tick), issues 5 s, settings 5 s, repository setup 3 s.

### Onboarding page (`setInterval` 3000 ms)
- Skips when `busy`, or when there is no `operation`, phase is not `resetting`, current step is not 3 (index 2) and the path is not `/settings/harnesses`. Re-renders only on signature change. Request errors persist across polls until the next action.

### Header activity pill (`navigation.py` ACTIVITY_SCRIPT)
- On every page: `GET /api/factory` (10 s timeout) immediately, then every 15 s after each attempt (fixed, no backoff); skipped while hidden; refreshed on becoming visible.
- Hide when `!ok`, `!active`, no `observation.at`, or `now - observation.at > maxAge` where `maxAge = max(120, (runtime.cycle_seconds||30)*3)`.
- Working jobs = `!cleanup && !fenced && !outcome && phase==='running' && (observed_at||last_observed) within maxAge`. Per role counts render as links: build → `/factory#stage-build` "N builder|builders working", review → `#stage-review` "reviewer(s)", audit → `#stage-audit` "auditor(s)", merge → `/factory#runners` "merge worker(s)"; recovery is ignored. Each shows a spinner + count; label in `title`/`aria-label`.
- Queued = issues `!active` with state ready/built/reviewed whose repo is enabled (when `repositories` is an array) → link `/factory#columns` text "N queued", title "Tracked issues waiting for Build, Review or Audit", aria-label "N queued issue|issues".
- Pill colors by role: build `#94bfff`, review `#c9a4f4`, audit `#80d7bd`, merge `#edbd77`.

---

## 5. Header and navigation (`forge/navigation.py`)
- Sticky top bar `.forge-topbar` (bg `#101416`, border `#303b40`): brand link "Forge" with rotated mark "F" (`aria-label="Forge home"`, href `/`); right side `#forge-activity` pill (role=status, aria-live polite) + `nav[aria-label="Main navigation"]` containing exactly two links: "Settings" (`/settings`, `aria-current="page"` on any `/settings*` path) and the help icon (`/setup-help`, `aria-label="Help"`, `title="Help"`, circled question mark SVG).
- Secondary nav: on `/`, `/factory`, `/issues` → `nav[aria-label="Work navigation"]` with "Activity" (`/factory`; current on `/` and `/factory`) and "Issues" (`/issues`). On `/settings*` → `nav[aria-label="Settings navigation"]` tabs "General" (`/settings`; also current for `/settings/advanced`), "GitHub access" (`/settings/github`; also current for `/settings/repository`), "Coding accounts" (`/settings/harnesses`). Active tab color `#bfe6cd` underlined.
- Settings pages get `body.forge-settings-page` (max-width 1200, eyebrow style, h1 32px/650). Pill styling is applied to internal links (`a[href^="/"]` excluding nav/brand/activity/eyebrow/issue-title/repo-link/download) and `.forge-nav-pill`/`.secondary-pill`: 32px min-height (44px ≤700px), radius 999px, border `#354147`, text `#c0cbcd`.
- `--forge-header-height` CSS variable measured by ResizeObserver; `scroll-padding-top` = header + 16px.
- Favicon: `<link rel="icon" type="image/svg+xml" sizes="any" href="/favicon.svg?v=1">`; SVG = 64×64 rounded square `#101416` with a rotated (−6°) `#bfe6cd` outlined square and "F" glyph.
- Role illustration sprite ids: `#forge-role-build`, `#forge-role-review`, `#forge-role-audit`, `#forge-role-controller`, `#forge-github-badge`; colors build `#94bfff`, review `#c9a4f4`, audit `#80d7bd`, controller `#edbd77`.
- Page titles: "Forge · Factory", "Forge · Issues", "Forge · Settings", "Forge · GitHub access", "Forge · Prepare repository", "Forge · Setup", "Forge · Setup help".
