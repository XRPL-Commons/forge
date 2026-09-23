# Deployment tracking — design

Status: approved in conversation with Luc on 2026-09-24. Implements the
"deploy strategist" idea: a merge count only means something if Forge knows
whether the merge is live, and nobody wants to type domains into settings.

## Goal

For every onboarded repository Forge learns, with one model-driven read of the
repository and at most one question to the operator, how to tell which commit
is running in production. It then reports, per issue, whether its merge is
deployed, and on the Activity page, how many merges went live today.

## Non-goals

- Forge never deploys anything and never changes deploy workflows.
- Forge never adds a new HTTP route to a product. Exposure of the running
  commit is limited to an existing health check or a response header.
- No per-repository settings form beyond the one domain question.

## Vocabulary

- **Deployment profile**: what Forge knows about how a repository deploys and
  how to observe the live commit. Stored per repository in settings state.
- **Strategist**: an internal job (role `strategist`) that reads a repository
  inside the normal runner sandbox and returns a deployment profile proposal.
- **Observation**: the supervisor's per-cycle read of GitHub deployment
  statuses and, when a domain and exposure are known, one HTTP request to the
  domain.

## The strategist job

A fifth role beside `recovery`, `audit`, `review`, `build`. It shares the
runner contract (`forge/factory/harness.py`, assignment note, result schema,
`/work/result.json`) and dispatch rules (memory admission, provider lease,
one slot `strategist: 1` in `runtime.slots`, default 1).

When it runs:

- once for every enabled repository that has no profile, or whose profile's
  `workflows_digest` no longer matches the digest of `.github/workflows/*` on
  the default branch;
- again when the operator presses "Re-read deployment" on the Repository page.

It never has a candidate and never publishes. Its checkout is the default
branch at the observed head; GitHub access is the read-only worker token.

### Result contract

One profile per repository: the onboarded repositories redeploy everything on
each push to main, so a single domain describes them. A monorepo with
independently deployed applications is out of scope for now; the profile
shape leaves room for a list of targets later.

`schema(note)` for `role == 'strategist'` adds these properties (all
required, `additionalProperties: false`):

```
strategy:      'github_deployments' | 'health' | 'header' | 'unobservable'
deploy:        { workflow: string|null, environment: string|null, trigger: string|null }
domain:        { value: string|null, source: 'workflow'|'docs'|'config'|'none', confidence: 'high'|'low'|'none' }
exposure:      { kind: 'health'|'header'|'none', path: string|null, field: string|null, header: string|null }
commit_source: string      # where the build learns its commit, e.g. "Dockerfile ARG GIT_SHA" or "not available"
proposal:      { title: string, body: string } | null
```

Rules the prompt states and the schema enforces:

1. `exposure.kind == 'health'` only when the application already serves a
   health or status endpoint; `path` and `field` name where the commit goes.
2. `exposure.kind == 'header'` when there is an HTTP surface but no health
   endpoint; `header` is `X-Build-Commit`.
3. `exposure.kind == 'none'` when the repository has no HTTP surface or the
   model could not tell. Then `strategy` is `github_deployments` when the
   repository has a deploy workflow with an `environment`, else
   `unobservable`.
4. `proposal` is non-null exactly when `exposure.kind == 'none'` and the
   repository has an HTTP surface: the issue text asks to expose the build
   commit in the existing health check if one exists, otherwise as the
   `X-Build-Commit` response header, set from a build-time variable that the
   deploy workflow already has (the image tag `sha-<commit>` is the hint).
5. `domain.value` is a bare host name, never a URL with a path. When the
   model finds none, `domain.source == 'none'` and the operator is asked.

### What Forge does with the result

`Ledger.strategy(job, result)` records the proposal on the repository entry of
the ledger (`data['deployments'][repo_id]`) with `observed_at`, the
`workflows_digest`, the head commit read, and `status: 'proposed'`.

The proposal is never applied silently. The Repository page shows it as a
"Deployment" card; the operator confirms once. Confirming writes the profile
into settings (`config['repositories'][i]['deployment']`, see below) and sets
`status: 'confirmed'`. Rejecting sets `status: 'rejected'` with a reason and
the strategist does not run again until "Re-read deployment" or a workflows
change.

When `proposal` is non-null, confirming also files that issue on the
repository through the controller App. The issue carries the label
`forge:deployment` and **not** the ready label: the operator starts it from
the Issues page when they want to. Forge records the filed issue number in the
profile (`exposure_issue`) so it is never filed twice.

## Settings

`config['repositories'][i]` gains an optional key `deployment`:

```
deployment: {
  strategy, deploy: {workflow, environment, trigger}, domain: string|null,
  exposure: {kind, path, field, header}, workflows_digest: string,
  confirmed_at: number, exposure_issue: int|null
}
```

`validate()` accepts the key when present and checks its shape; rows without
it stay valid. Revision hashing includes it, so the existing optimistic
concurrency covers edits. The domain is the one thing an operator types: the
Deployment card on the Repository page shows a domain box prefilled with the
strategist's candidate when it found one. A profile with no domain is
observed through GitHub deployments only.

## Observation

Each supervisor cycle, inside the existing budget and after issue
reconciliation, `Supervisor.observe_deployments()`:

1. For each confirmed profile whose `deploy.environment` is set: `GET /repos/{repo}/deployments?environment=<env>&per_page=5` and the
   latest status of the newest deployment (`success`, `failure`,
   `in_progress`, …). This needs `deployments: read` on the Controller App
   (`ROLE_PERMISSIONS['controller']` gains it; the GitHub access page shows
   the missing permission like any other).
2. When `exposure.kind != 'none'` and `domain` is set: one request to
   `https://<domain><path>` (GET for `health`, HEAD for `header`), timeout
   5 s, response body capped at 64 KiB, no redirects across hosts. The commit
   read is the 40-hex value at `field` or in `header`.
3. The result is stored on the ledger repository entry:
   `live: {commit, at}`, `deploy: {sha, state, at, run_url}`, `error`.

Then, for every issue in state `merged` whose merge commit is known and that
is not yet marked deployed: **live** when `live.commit` equals the merge
commit or the merge commit is an ancestor of it
(`GET /repos/{repo}/compare/{merge}...{live}`, `status` in `ahead`/`identical`,
cached per pair) marks `deployed: {commit, at, via: 'live'}`; otherwise a
GitHub deployment whose `sha` contains the merge commit with latest status
`success` marks `deployed: {via: 'github'}`; a latest status of `failure`
marks `deploy_failed: {sha, at, run_url}` until a later deployment succeeds;
anything else is waiting.

Errors reading a domain or the deployments API are recorded per repository
and surfaced as one warning line on the Activity page ("Deployment
observation failed for academy: connection timed out"), never as a false
"not deployed".

## Operator UI

- **Repository page**, new "Deployment" card under the protection card:
  strategy and deploy workflow as read by the strategist, a domain box, the
  exposure line ("commit read from /health `commit`" or "header
  `X-Build-Commit`" or "not exposed — issue #N filed / to be filed"), the
  observation status ("live commit abc1234 · deploy succeeded 12 min ago"),
  and the buttons "Confirm" and "Re-read deployment".
- **Activity page**: the Merged row becomes "Deployed today · N" with the
  meaning "M merged and waiting · K deploy failed"; without any confirmed
  profile it stays "Merged today" with "deployment not tracked". Completed
  cards show "deployed", "waiting for deploy" or "deploy failed".
- **Issue detail**: a "Deployment" line with the same three states and the
  deploy run link when there is one.
- **GitHub access page**: `deployments: read` appears in the controller's
  missing permissions until granted.

## Failure and safety rules

- The strategist's output is a proposal; nothing changes settings, files an
  issue, or contacts a domain until the operator confirms.
- Domain requests carry no credentials, follow no cross-host redirects, and
  read at most 64 KiB. A domain that fails three cycles in a row is retried
  every 10 minutes instead of every cycle.
- `deployed` is only ever set from a commit Forge read itself or from a
  GitHub deployment status; a merge with no evidence stays "waiting".
- The strategist has the recovery role's failure handling: an `unable` or
  malformed result records `status: 'failed'` with the summary, and the
  Repository card offers "Re-read deployment".

## Tests

Python: schema for the strategist role; `Ledger.strategy` recording;
settings validation of `deployment`; `observe_deployments` with a fake
GitHub and a fake HTTP reader covering live match, ancestor match, deployment
success, deployment failure, domain timeout; issue filing without the ready
label and idempotence via `exposure_issue`. UI: `breakdown()` deployed row,
Repository card states in unit tests, one e2e scenario `deployment/` with a
proposed profile and a confirmed profile.
