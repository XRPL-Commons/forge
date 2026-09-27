# Software factory contract

For lifecycle diagrams, waiting reasons and operator actions, see the
[issue state machine and blockers](issue-state-machine.md).

Implementation: `forge/factory`. This contract supersedes the earlier three
rejected-build-cycle design. Deployment is an explicit cutover; the legacy fleet
is not changed by installing source. See [runtime setup](factory-runtime.md) for
concrete defaults, host policy, authentication and verification.

## Authority

GitHub owns requirements, code, PRs, review evidence, protections and merge facts.
The factory ledger owns assignments, attempts, accepted milestones, failure budgets
and reservations. Labels are only a projection of reconciled state.

Only the deterministic supervisor changes internal issue state or pipeline labels.
Agents return evidence. A role-agnostic runner owns execution, log capture,
deadlines and teardown; its terminal/output is an inspection surface, not truth
about task completion. A completed execution does not establish a merged issue.

## Issue transitions

| Milestone | Action/actor | Required output | Accepted transition | Failure/remediation |
| --- | --- | --- | --- | --- |
| ready | Build agent | Explicitly submitted PR matching its persisted assignment | built | Failed publication/execution → investigate → Build |
| built | Review agent | Revision-bound verdict, published review and role check | reviewed | Code rejection → investigate → Build; execution failure → investigate → Review |
| reviewed | Audit agent | Revision-bound technical verdict plus separate human requirement; published review/check | audited | Code rejection → investigate → Build; execution failure → investigate → Audit |
| audited | Supervisor | Fresh checks, protection and human-policy verification | merge request or approval_needed | Conflict → investigate → Build; unknown/waiting checks → observe |
| approval_needed | Supervisor observes GitHub | Current authorized human approval and native protection | merge request | Approval revoked, code/base/policy/requirements changed → revalidate |
| merge request | Supervisor | GitHub confirms the PR merged | merged | Unknown response → reconcile; confirmed conflict → investigate → Build |
| investigating | Recovery agent | Cause, evidence, uncertainty, recommended alternative | Retry previous verified milestone or hold | Investigation failure records incomplete findings; no recursive investigation |
| hold / failed / cancelled | Human decision, applied by supervisor | Explicit reason and new budget decision | ready | Relabeling does not reset anything |

Build → Review → Audit is sequential for one candidate. Different issues run
concurrently. The supervisor reconciles existing executions, resets finished or
broken ones, resolves evidence and investigations, then allocates available slots
in Recovery → Audit → Review → Build order. Triage runs only when a builder slot
is free, discovering open issues with `ready`, respecting holds, access and existing
ownership. Eligible issues use stable oldest-discovered/repository/issue ordering.

Verify the shared two-review and stale-approval baseline before dispatch.
Persist the assignment and reserve capacity before launching. One issue cannot
receive replacement work while its old execution has unverified cleanup. A
provider outage pauses dispatch without charging issues. The supervisor's GitHub
work is bounded and resumes on subsequent cycles; teardown is independent of API
availability. Closing or merging an issue externally is reconciled even when its
previous attempt failed, so history cannot remain a stale attention alert.

## GitHub publication

The builder receives issue/repository identity, a unique attempt ID, assigned branch,
candidate if present, deadline and compact recent lifecycle findings. It reads the
issue comments and repository context as needed. It may edit its branch, create or
update its PR, and comment on its issue. It may not close/rewrite the issue, change
pipeline labels, merge, or launch another worker.

Use one PR-body block, not an additional submission comment:

```text
<!-- forge-submission {"version":1,"issue":123,"attempt":"<assigned UUID>","head":"<submitted SHA>"} -->
```

An unfinished build uses `head: null` and a draft PR. Submission pushes the final
code, records its exact head, and marks the PR ready last. Rework normally reuses
the PR, first returning it to draft and replacing its attempt/head metadata.

The supervisor independently checks repository ID, issue, attempt, assigned branch,
expected builder publisher, assigned target branch, open/non-draft state and current head. Exactly one
matching candidate is required. Titles, conversational links or a success message
are insufficient. A verified submission can be recovered after a crash. A success
report without publication consumes one failure. Unknown outcomes are reconciled
before retry; ambiguity never authorizes deletion of a GitHub PR or branch.

Candidate identity includes repository, issue, PR, head, evaluated base, build
attempt, issue requirements and factory policy. The requirements digest includes
the issue title/body and human issue comments, excluding bot projection noise.
Changing these facts invalidates old evidence without itself consuming a failure.
A new head must satisfy submission before fresh Review/Audit. A changed base or
policy on a still-valid submission starts fresh Review/Audit.

## Deployment observation

A deployment profile is a proposal until an operator confirms it. The
strategist role reads a repository read-only and returns a candidate strategy,
deploy workflow, domain and exposure; it never has a candidate, never
publishes, and nothing it returns changes settings, files an issue or
contacts a domain by itself. Confirming is the operator's decision alone, made
on the Repository page, and it is the only thing that writes the profile into
repository settings.

Forge never deploys a repository and never changes how one deploys. The
supervisor's observation loop reads GitHub deployment statuses for the
confirmed profile's environment and, when a domain is set, one HTTPS request
to it. A merged issue becomes `deployed` only from a commit Forge read
itself — live or an ancestor of it, or carried by a successful GitHub
deployment — never from elapsed time, execution success or the absence of
contrary evidence; without that evidence it stays `waiting`.

When confirming a profile also files a deployment issue, that issue carries
the label `forge:deployment` and not the ready label. Filing it is not
queuing it: Forge never dispatches a builder to it on its own, the same as
any other issue without the ready label. The operator adds it when they want
the work done.

## Verdict and approval contract

Workers return one bounded JSON result file bound to the assignment. Common fields
are `version`, `attempt`, `role`, `outcome`, `summary`. Review/Audit include the
candidate key. Audit additionally reports `human_required` (`required`,
`not_required`, `unknown`) and `human_reason`. Recovery reports `pattern`,
`evidence`, `uncertainty`, and an optional alternative with `change`, `reason`,
and supporting `evidence`. Invalid/partial output is not a successful verdict.

Review/Audit workers have read-only repository tokens. The supervisor verifies
revision identity, publishes their verdict under the respective App, verifies that
publication, and then accepts the milestone. Publications carry an attempt or
candidate idempotency key and are reconciled before repeating uncertain writes.

Publish App-bound checks `forge/review`, `forge/audit`, `forge/merge-policy` on
Forge PRs and verify them inside the supervisor. They need not be globally required
branch checks. Developers keep their normal repository access and merge paths;
Forge reconciles their changes instead of claiming exclusive ownership.
Two approval counts alone do not prove the two technical roles passed. The auditor
may require additional human approval but may not waive repository policy.
Human approval supplements a passing technical audit; it never overrides failure.

A human wait releases the auditor and consumes no failure. The supervisor observes
native reviews, excluding bots and the PR author, requiring current-candidate
approval from configured humans and rejecting active requests for changes.
Approval must be newer than acceptance of the current candidate; same-head
requirements/policy changes cannot inherit an old human decision.

Head changes invalidate prior approvals; require GitHub stale-approval dismissal.
Before merge, verify current named-check producers/results, exact head/base,
requirements, policy, and native protections again. Merge with an expected head
and no bypass; only a verified GitHub merge establishes `merged`.

For human-escalated automatic merges, the first adapter additionally requires a
native wildcard CODEOWNERS rule containing only configured humans and required
code-owner reviews. This lets GitHub reject a merge when approval is dismissed
during the controller's final read/request race. This native rule is repository-wide;
Required code-owner reviews can come from classic protection or active rulesets.
More selective CODEOWNERS/team and merge-queue variants need explicit verified adapters.
Unsupported policy is held rather than assumed equivalent.

## Failure rule

Count three failed attempts total across Build, Review, Audit and Merge. Successful
steps do not reset the count. Distinguish code rejection, execution and publication
failure in history, but use one budget. Charge each failed attempt once; cleanup
errors and duplicate observations do not add another failure.

A confirmed rejection/conflict returns to Build after investigation. Execution
failure retries the affected step from its last verified milestone. Discard
unfinished disposable work; preserve verified publications and durable evidence.
Capacity/provider waits, human approval waits, external candidate changes and
explicit cancellation do not charge the budget.

Every confirmed failure gets one investigation, including the third failure.
Recovery reads preserved diagnostics, issue history and GitHub facts. It cannot
edit code/GitHub state, reset budgets, merge or start jobs. Recovery failure records
an incomplete finding without another issue charge or another investigation.
Reset does not wait for investigation. Independently verified recovery of a
submitted PR corrects that original failure charge once.

After two matching underlying patterns, an unchanged third attempt is forbidden.
The next assignment must contain a concrete alternative supported by evidence.
Missing/uncertain findings or no useful alternative cause an early human hold.
A fresh session or rephrased instruction is not an alternative. Three failures
always enter `failed`; the escalation history includes causes, attempted changes
and unresolved help. A human retry records its decision, grants a new budget and
retains all previous attempts. Stronger-model escalation is not an automatic retry.

## Runner contract

Each reusable slot receives a unique execution UUID and disposable container for
each job. Its durable assignment, container ownership and teardown intent live
outside the worker environment. The underlying coding harness is a parameter;
role-specific acceptance belongs to the supervisor.

Execution phases are `reserved → created → launching → running → stopping → cleaned`.
Crashes at every phase resume reconciliation under the same identity. Never reuse
a terminal name, PID or worktree as authority. Persisted launch intent precedes
start; uncertain start is inspected before any further action.

The image-owned runner emits heartbeats, captures output, enforces an absolute job
deadline and detects exit. Heartbeat, process existence, progress and GitHub success
remain separate observations. Its external controller uses the container manager
and private PID namespace to tear down background/detached children as well as the
main harness.

Reset, on every exit path:

1. Persist fencing of the old execution and stop accepting its authority.
2. Capture bounded evidence best-effort; missing logs do not block reset.
3. Request bounded graceful stop, then force removal of the owned execution.
4. Remove its anonymous workspace volume and verify container absence and engine
   availability. Fresh dispatch verifies image/network/policy readiness and creates
   a new environment.
5. Record teardown evidence and release only that execution's reservation.

Reset is repeat-safe and targets the exact recorded identity; stale requests cannot
stop or release a replacement. An unreachable engine is unknown, not empty. Keep
affected capacity unavailable until teardown can be verified while other capacity
continues. Successful publication survives a cleanup failure. Reset does not erase
GitHub artifacts or charge another issue failure.

## Configuration and migration

Humans register/install the role Apps and select repositories on GitHub. Forge
synchronizes grants/repositories periodically and on demand, separating missing
access from unknown observations. Discovery never enables automation; activation
is explicit and bound to numeric repository identity. Multiple installations per
role support repositories across accounts. Credentials are referenced, not stored
in editable source/configuration; workers never inherit App private keys.

`factory.json` and `runtime.json` are the same authoritative files for humans,
agents and Settings. UI saves validate and reject stale revisions. Defaults/examples
are portable and disabled; private runtime files are excluded from public source.

Drain legacy executions and reconcile their GitHub work before cutover. Keep old
history; do not infer new milestones or budgets from ambiguous legacy state.
Controller modes share one lock and a persistent selection marker. A new install
must not share repositories with another independent controller root.

After observing the assigned PR as merged, the supervisor closes the GitHub issue
as completed and reconciles its final pipeline label. Failed or unknown API writes
are retried as reconciliation; they do not undo an observed merge or spend a failure.
