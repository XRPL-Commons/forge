# Deployment tracking — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Forge learns per repository how to see which commit is live, asks the operator for one domain, and reports per issue whether its merge is deployed.

**Architecture:** A `strategist` job (probe-style container driven by the setup service, not an issue job) reads a repository and returns a deployment profile proposal; the operator confirms it on the Repository page (one domain box); the supervisor observes GitHub deployment statuses and the domain each cycle and marks merged issues deployed. State lives in `state/deployments.json` (proposals, observations) and in settings (`repositories[i].deployment`, the confirmed profile).

**Tech Stack:** Python 3.12 stdlib (existing `forge/` package, `AtomicStore`, `ContainerRunner`, `GitHub` wrapper), Nuxt 4 + Nuxt UI 4 (`ui/`), unittest + vitest + the e2e mock API.

**Spec:** `docs/superpowers/specs/2026-09-24-deployment-tracking-design.md`

## Global Constraints

- Forge never deploys, never edits deploy workflows, never adds an HTTP route to a product.
- The strategist's result is a proposal; nothing changes settings, files an issue or contacts a domain before the operator confirms.
- The filed issue carries label `forge:deployment` and never the ready label.
- Domain reads: HTTPS only, timeout 5 s, body ≤ 64 KiB, no cross-host redirects, no credentials, GET for `health`, HEAD for `header`.
- `deployed` is set only from a commit Forge read itself or a GitHub deployment status; otherwise the issue is "waiting".
- Controller App permission `deployments: read` is required for deployment statuses; missing permission is reported, never fatal.
- Credential contents are never printed or logged. Error messages stay credential-free like the rest of `forge/`.
- Python tests run with `python3 -m unittest <modules>`; UI with `cd ui && npm run typecheck && npm test && npm run test:e2e` (e2e needs `FORGE_CHROMIUM_SANDBOX=0` and `FORGE_BROWSER_PATH` locally; ports 8788/3999 must be free).
- Commit after each task with a signed commit (`git -c commit.gpgsign=true commit`).

---

### Task 1: Strategist result contract and harness note

**Files:**
- Modify: `forge/factory/harness.py` (`schema()`, checkout for role `strategist`)
- Modify: `forge/factory/contracts.py` (add `strategist_note()`)
- Test: `tests/test_factory_adapters.py` (or a new `tests/test_strategist_contract.py`)

**Interfaces:**
- Produces: `contracts.strategist_note(job) -> dict` with keys `id, role='strategist', repo, repo_id, publisher, target_branch, candidate=None, deadline, result_file, instructions, head` where `head` is the default-branch commit the strategist must read (the note is written by Task 2 into `/work/assignment.json`).
- Produces: `harness.schema(note)` for `role == 'strategist'` returning exactly the spec's properties (`strategy, deploy, domain, exposure, commit_source, proposal`, plus the shared `version, attempt, role, outcome, blocker, summary`).
- Consumes: nothing new.

- [ ] **Step 1: Write the failing tests**

```python
class StrategistContractTests(unittest.TestCase):
    def test_schema_lists_the_profile_fields(self):
        note = {'id': 'job-1', 'role': 'strategist', 'candidate': None}
        value = schema(note)
        self.assertEqual(set(value['required']), {'version', 'attempt', 'role', 'outcome', 'blocker', 'summary',
                                                  'strategy', 'deploy', 'domain', 'exposure', 'commit_source', 'proposal'})
        self.assertEqual(value['properties']['strategy']['enum'], ['github_deployments', 'health', 'header', 'unobservable'])
        self.assertEqual(value['properties']['exposure']['properties']['kind']['enum'], ['health', 'header', 'none'])
        self.assertFalse(value['additionalProperties'])

    def test_note_carries_read_only_instructions_and_head(self):
        note = strategist_note({'id': 'job-1', 'repo': 'x/y', 'repo_id': 7, 'head': 'a' * 40, 'deadline': 10})
        self.assertEqual(note['role'], 'strategist'); self.assertIsNone(note['candidate'])
        self.assertIn('Read only', note['instructions']); self.assertIn('X-Build-Commit', note['instructions'])
        self.assertEqual(note['head'], 'a' * 40); self.assertEqual(note['result_file'], '/work/result.json')
```

- [ ] **Step 2: Run to verify they fail** — `python3 -m unittest tests.test_strategist_contract -v`

- [ ] **Step 3: Implement**

In `harness.py::schema`, add a branch:

```python
    if note['role'] == 'strategist':
        nullable = lambda t: {'type': [t, 'null']}
        props.update(
            strategy={'type': 'string', 'enum': ['github_deployments', 'health', 'header', 'unobservable']},
            deploy={'type': 'object', 'properties': {'workflow': nullable('string'), 'environment': nullable('string'), 'trigger': nullable('string')},
                    'required': ['workflow', 'environment', 'trigger'], 'additionalProperties': False},
            domain={'type': 'object', 'properties': {'value': nullable('string'), 'source': {'type': 'string', 'enum': ['workflow', 'docs', 'config', 'none']},
                    'confidence': {'type': 'string', 'enum': ['high', 'low', 'none']}}, 'required': ['value', 'source', 'confidence'], 'additionalProperties': False},
            exposure={'type': 'object', 'properties': {'kind': {'type': 'string', 'enum': ['health', 'header', 'none']}, 'path': nullable('string'),
                      'field': nullable('string'), 'header': nullable('string')}, 'required': ['kind', 'path', 'field', 'header'], 'additionalProperties': False},
            commit_source={'type': 'string'},
            proposal={'anyOf': [{'type': 'null'}, {'type': 'object', 'properties': {'title': {'type': 'string'}, 'body': {'type': 'string'}},
                      'required': ['title', 'body'], 'additionalProperties': False}]})
```

In `harness.py::main`, when `note['role'] == 'strategist'`, fetch and detach at `note['head']` instead of `origin/HEAD` (the note has no candidate). Never create a branch.

In `contracts.py`, add `strategist_note(job)` next to `assignment_note`. Instructions (one string): read only; identify the deploy workflow, its trigger and `environment`; find where the app is served (domain) in workflows, docs or config and report the source and confidence; find an existing health/status endpoint and, if present, name the JSON field where the commit should go; otherwise propose the `X-Build-Commit` response header set from a build-time variable the deploy workflow already has; never propose a new route; produce a `proposal` issue text only when nothing exposes the commit and the app has an HTTP surface; write the result contract atomically to `/work/result.json`; never include credentials.

- [ ] **Step 4: Run the tests** — `python3 -m unittest tests.test_strategist_contract tests.test_factory_adapters tests.test_build_environment`
- [ ] **Step 5: Commit** — `git add forge/factory/harness.py forge/factory/contracts.py tests/test_strategist_contract.py && git -c commit.gpgsign=true commit -m "Strategist role: result contract and harness note"`

---

### Task 2: Deployment store and the strategist runner

**Files:**
- Create: `forge/deployments.py`
- Modify: `forge/setup_service.py` (call `Deployments(self.root, ...).tick(...)` from `Service.tick` once per cycle, after the supervisor cycle, only when `phase == 'active'` and no setup operation is pending)
- Modify: `forge/factory/github.py` (add `workflows_digest(item) -> (digest, head_sha)` reading `GET /contents/.github/workflows` and the default branch head via `GET /branches/<default>`)
- Test: `tests/test_deployments.py`

**Interfaces:**
- Produces: `class Deployments(root, *, runner, github, settings, clock=time.time)` with:
  - `read() -> {'version': 1, 'repositories': {str(repo_id): entry}}` from `AtomicStore(root/'state', 'deployments')`.
  - entry = `{'name', 'status': 'idle'|'running'|'proposed'|'confirmed'|'rejected'|'failed', 'workflows_digest', 'head', 'job', 'proposal', 'summary', 'error', 'observed_at', 'reread_requested', 'observation': {...}}`.
  - `tick(config)`: (1) for enabled repositories with access (`settings.view()['repositories']`, `dispatch_allowed`), compute `workflows_digest` at most every 30 min per repo; if no entry, or `reread_requested`, or digest changed and status not `running`, start a strategist job when none is running; (2) drive the running job through `runner.inspect/create/prepare/start/capture/reset` exactly like the provider probe in `Service.check`, using a worker token from `github.token('controller', repo_id, worker=True)` for `prepare`; (3) on exit, parse `capture['result']` with `harness.schema`-shaped validation (reuse `contracts.result`-style checks: version 1, attempt id, role), store `proposal` + `summary` with `status: 'proposed'`, or `status: 'failed'` with the summary/timeout reason.
  - `request_reread(repo_id)`, `confirm(repo_id, domain) -> profile dict` (returns the profile to write into settings; the store keeps `status: 'confirmed'`), `reject(repo_id, reason)`.
  - `new_job(config, repo, head, deadline)`: like `Service.new_probe` but `role='strategist'`, `slot='deployment-strategist'`, `repo`/`repo_id` set, `publisher='forge-strategist'`, `harness_argv=config['harness_argv']`, `kind='strategist'`, memory from config.
  - The runner's `prepare(job, token)` reads `assignment_note(job)` today; make `ContainerRunner.prepare` call `strategist_note(job)` when `job['role'] == 'strategist'` (one branch in `runner.py`).
- Consumes: Task 1's `strategist_note`, `schema`.

- [ ] **Step 1: Write the failing tests** (fake runner like `tests/test_onboarding.py::SetupTests.service`, fake GitHub returning a fixed `workflows_digest`)

```python
def test_first_tick_starts_one_strategist_per_repository_and_records_its_proposal(self):
    deployments.tick(config)            # creates + starts the job for repo 7
    self.assertEqual(deployments.read()['repositories']['7']['status'], 'running')
    engine[job_id].update(status='exited', running=False, exit_code=0)
    runner_results[job_id] = {'version': 1, 'attempt': job_id, 'role': 'strategist', 'outcome': 'pass', 'blocker': None,
        'summary': 'deploy.yaml pushes to production', 'strategy': 'header',
        'deploy': {'workflow': 'deploy.yaml', 'environment': 'production', 'trigger': 'push:main'},
        'domain': {'value': 'academy.xrpl-commons.org', 'source': 'workflow', 'confidence': 'high'},
        'exposure': {'kind': 'none', 'path': None, 'field': None, 'header': None}, 'commit_source': 'image tag sha-<commit>',
        'proposal': {'title': 'Expose the build commit', 'body': '...'}}
    deployments.tick(config)
    entry = deployments.read()['repositories']['7']
    self.assertEqual(entry['status'], 'proposed'); self.assertEqual(entry['proposal']['domain']['value'], 'academy.xrpl-commons.org')

def test_only_one_strategist_runs_at_a_time(self): ...
def test_workflows_change_reruns_and_reread_reruns(self): ...
def test_malformed_result_marks_failed_and_offers_reread(self): ...
def test_confirm_returns_profile_with_operator_domain_and_reject_stops_reruns(self): ...
def test_tick_is_skipped_while_a_setup_operation_is_pending(self): ...
```

- [ ] **Step 2: Run to verify they fail**
- [ ] **Step 3: Implement** `forge/deployments.py`, the `runner.prepare` branch, `GitHub.workflows_digest`, and the `Service.tick` hook (guard with try/except logging the error into the store entry, never breaking the supervisor cycle).
- [ ] **Step 4: Run** `python3 -m unittest tests.test_deployments tests.test_onboarding tests.test_harnesses tests.test_factory_adapters`
- [ ] **Step 5: Commit** — "Deployment store and strategist runner"

---

### Task 3: Settings profile, repository views and the operator API

**Files:**
- Modify: `forge/settings.py` (`validate`: optional `deployment` key on repository rows; keep `set(row) - {'deployment'} == {'id','name','enabled'}`)
- Modify: `forge/repository_setup.py` (`views()` adds `deployment` = confirmed profile from settings merged with the store entry: `{'status', 'proposal', 'profile', 'observation', 'error', 'summary', 'exposure_issue'}`)
- Modify: `legacy/forge-status.py` (POST `/api/repositories/deployment` with body `{repo_id, revision, action: 'confirm'|'reject'|'reread', domain?, reason?}`; allowed-path set; same guards as the other POSTs)
- Modify: `forge/factory/github.py` (`file_issue(item, title, body, labels) -> number` via `POST /issues`, controller role)
- Modify: `forge/app_access.py` (`ROLE_PERMISSIONS['controller']['deployments'] = 'read'`)
- Modify: `docs/superpowers/specs/2026-09-22-operator-api-contracts.md` (§2: `RepositoryRow.deployment`, the new POST)
- Test: `tests/test_settings.py`, `tests/test_repository_setup.py`, `tests/test_settings_http.py` (whichever hosts POST handler tests; follow the existing pattern)

**Interfaces:**
- Produces: `repositories[i].deployment` (spec shape). `confirm` writes the profile through `Settings.save(config, revision)` so the revision check applies; then files the proposal issue when `exposure.kind == 'none'` and `proposal` is set and `exposure_issue` is null, storing the number. `reject` and `reread` only touch the store.
- Consumes: Task 2's `Deployments.confirm/reject/request_reread`.

- [ ] **Step 1: Failing tests** — validation accepts/rejects the shape; `views()` merges store + settings; POST confirm writes settings, files the issue exactly once with labels `['forge:deployment']` and never the ready label, returns `{ok, revision}`; POST with a stale revision is refused with the existing message.
- [ ] **Step 2: Run to verify they fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run** the three test modules plus `tests.test_factory_adapters` (permission table test).
- [ ] **Step 5: Commit** — "Deployment profile in settings; confirm, reject and re-read API"

---

### Task 4: Observation and issue verdicts

**Files:**
- Modify: `forge/deployments.py` (`observe(github, ledger, reader)`)
- Create: `forge/factory/live.py` (`read_commit(url, *, kind, field=None, header=None, timeout=5, limit=65536) -> str|None`; stdlib `urllib`, HTTPS only, refuses redirects to another host, GET/HEAD per kind, extracts a 40-hex commit)
- Modify: `forge/factory/github.py` (`deployments(item, environment, limit=5)`, `deployment_statuses(item, deployment_id)`, `contains(item, ancestor, descendant) -> bool` via `GET /compare/{ancestor}...{descendant}` with `status in ('ahead','identical')`)
- Modify: `forge/factory/ledger.py` (`deployed(key, verdict)` and `deploy_failed(key, info)` writing `issue['deployed']` / `issue['deploy_failed']` and an event `deployed` / `deploy_failed`)
- Modify: `forge/factory/supervisor.py` (call `Deployments(...).observe(self.github, self.ledger, read_commit)` inside `cycle()` after the issue loop, still bounded by `observe_until`; per-repo errors go to `self.errors` with `phase: 'deployment'`)
- Modify: `forge/factory/control.py` (factory view: include `deployed`, `deploy_failed` on issues and a `deployments` map per repo `{live, deploy, error}`)
- Test: `tests/test_deployments.py` (observation cases), `tests/test_live.py` (reader with a fake opener)

**Interfaces:**
- Produces: issue fields `deployed: {commit, at, via}` / `deploy_failed: {sha, at, run_url}`; factory view `deployments`.
- Consumes: Task 3's confirmed profiles.

- [ ] **Step 1: Failing tests** — live equal commit → `deployed via live`; ancestor (compare `ahead`) → deployed; GitHub success containing the merge → `deployed via github`; failure → `deploy_failed`; timeout → error recorded, issue untouched, backoff after three failures; no profile → nothing happens; reader refuses `http://` and cross-host redirects and stops at 64 KiB.
- [ ] **Step 2: Run to verify they fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run** `python3 -m unittest tests.test_deployments tests.test_live tests.test_factory tests.test_factory_cycle`
- [ ] **Step 5: Commit** — "Observe deployments and mark merged issues deployed"

---

### Task 5: Operator UI

**Files:**
- Modify: `ui/app/utils/repositories.ts` (types), `ui/app/utils/factory.ts` (`LedgerIssueView.deployed/deploy_failed`, `FactoryView.deployments`, `breakdown()` merged row → "Deployed today" when any profile is confirmed)
- Create: `ui/app/components/DeploymentCard.vue` (Repository page card: status line, domain box, exposure line, Confirm / Reject / Re-read buttons, observation line)
- Modify: `ui/app/pages/settings/repository.vue` (mount the card under the protection card; actions post to `/api/repositories/deployment`)
- Modify: `ui/app/pages/issues/[key].vue` (a "Deployment" line: deployed · waiting for deploy · deploy failed with run link)
- Modify: `ui/app/components/IssueCard.vue` note for completed cards (footer note text only; no new rows)
- Fixtures: `ui/tests/e2e/fixtures/deployment/` scenario (`repositories.json` with a proposed profile, and a confirmed one; `factory.json` with a deployed and a waiting merged issue), README entry; `deployment.post.json` overrides
- Tests: `ui/tests/unit/factory.test.ts`, `ui/tests/unit/repositories.test.ts`, new `ui/tests/e2e/deployment.spec.mjs`

- [ ] **Step 1: Failing unit tests** — `breakdown()` deployed row and meanings (`2 merged and waiting · 1 deploy failed`); repository card state helper (proposed → shows domain box prefilled; confirmed → observation line; failed → re-read).
- [ ] **Step 2: Failing e2e** — open `/settings/repository?repo_id=1` in scenario `deployment`: card shows the proposal, confirm posts `{repo_id:1, revision, action:'confirm', domain:'academy.xrpl-commons.org'}`; Activity shows "Deployed today"; issue detail shows "deployed".
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run** `cd ui && npm run typecheck && npm test && npm run test:e2e`
- [ ] **Step 5: Commit** — "Deployment card, deployed row and issue deployment line"

---

### Task 6: Documentation

**Files:**
- Modify: `docs/software-factory-spec.md` (new section "Deployment observation" after "GitHub publication"), `docs/factory-runtime.md` (strategist job, `deployments: read`), `docs/onboarding.md` (the domain question), `docs/ui-deployment.md` (nothing new unless a runtime env var was added)

- [ ] **Step 1: Write the sections** from the spec, in the repository's voice (short declaratives, no marketing).
- [ ] **Step 2: Commit** — "Docs: deployment observation"

---

## Self-review notes

- Spec coverage: strategist (T1, T2), proposal confirmation + issue filing (T3), observation + verdicts (T4), UI (T5), permissions (T3), docs (T6).
- Type consistency: `Deployments.confirm(repo_id, domain)` returns the profile dict that T3 writes; T4 reads `profile['exposure']`, `profile['domain']`, `profile['deploy']['environment']` exactly as stored.
- Out of scope, by decision: multiple targets per repository.
