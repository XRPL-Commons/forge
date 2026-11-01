# Software factory contract

For lifecycle diagrams, waiting reasons and operator actions, see the
[issue state machine and blockers](issue-state-machine.md).

Implementation: `forge/factory`. This contract supersedes the earlier three
rejected-build-cycle design and the tmux fleet it ran on. See [runtime setup](factory-runtime.md) for
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

SQLite records the delivery stage separately from scoped waiting gates. Issue
labels and operator status are projections of that state.

| Delivery stage | Actor and evidence | Accepted progress |
| --- | --- | --- |
| `queued` / `building` | Singleton Build publication, or owner plan plus exact accepted child outputs | Singleton enters `reviewing`; a group becomes eligible for owner integration |
| `integrating` | Owning builder publishes one exact combined candidate containing every approved input | `reviewing` |
| `reviewing` | Independent revision-bound Review verdict and verified App evidence | `auditing` |
| `auditing` | Independent technical Audit verdict and separate human/verification requirements | `merging`, with any remaining gates |
| `merging` | Fresh member requirements, prerequisites, ownership, protections, CI, policy and approval evidence | A durable merge request; `merged` only after GitHub confirmation |
| Any live stage with a scoped gate | Required investigation, observation or explicit operator decision | Resume only the eligible scope; preserve other gates and the shared budget |
| `closed` / `dissolved` | Explicit lifecycle decision with cleanup and uncertain-effect guards | Retained history; no fabricated merge |

Build → Review → Audit is sequential for one candidate. An approved delivery may
contain several issues in one repository and base branch. One logical builder
owns that delivery across planning and integration executions; tracked child jobs
implement independent parts in separate checkouts. Dependencies pin exact accepted
outputs. Review and Audit each assess the complete current candidate. Changed
requirements, membership, head, base or policy require affected evidence again.

The factory coordinator enforces shared capacity, provider leases, cleanup, scoped
gates and the delivery's three-failure budget. Repository supervisors keep bounded
planning context and propose work through typed operations. Their notes guide
planning; current structured state and independent observations establish facts.
Every composition, including existing PRs, requires operator approval. Existing
PR choice pins source heads/authors and one publication target; it grants no
permission to close the other PRs.

Repository planning and ordinary ready-issue admission are separate operations.
Planning runs on explicit request or its schedule, after pipeline work, and
publishes a proposal. Accepting rows records local decisions atomically, with
GitHub edits executed later through durable effect identities. See
[repository planning](repository-planning.md) and
[delivery execution](delivery-execution.md) for their execution contracts.

Verify repository identity, App access and current rule observations before dispatch.
Persist the assignment and reserve capacity before launching. An assigned member cannot receive replacement work while its old execution has
unverified cleanup. Independent member work may continue when its own gates and
the shared budget permit it. A
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

Grouped integration uses a version-2 marker naming the exact delivery and sorted
member numbers, plus explicit closing references for every member. When updating
an existing PR, it marks the PR draft, writes the final attempt/head marker before
pushing that exact commit, then marks it ready last. This avoids a null-head gap
in the approved ownership check. Only the owning integration can publish a grouped
candidate; child results are independently verified commit bundles.

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

Any worker role can report `needs_human_verification` with bounded `verification`
fields: `what`, `why`, and optional configured CI `checks`. A valid published
candidate is required. Forge pauses at `approval_needed`, records the exact steps,
and resumes the remaining technical reviews only after verification. Approval
must be newer than the request; it cannot stand in for a passing technical verdict.
See [external verification](human-verification.md) for the contract and threat model.

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

Head changes invalidate prior Forge approval evidence.
Before merge, verify current named-check producers/results, exact head/base,
requirements, policy, and native protections again. Merge with an expected head
through the normal merge endpoint, which enforces the owner's GitHub rules.
Forge neither requires a baseline ruleset nor configures bypass grants.
Only a verified GitHub merge establishes `merged`.

Human-escalated automatic merges use the same eligible, current-candidate human
approval checks. Forge re-reads those approvals immediately before requesting the
merge. This read and GitHub's merge API are separate operations; the expected
head prevents a changed commit from merging but does not atomically bind a review.
Owners can use GitHub protections for additional enforcement. Forge does not
require a wildcard CODEOWNERS rule or configure owner protections.

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

Configuration is owned by the selected database and its typed revision checks.
UI, CLI and supervisor adapters use that same authority. Repository merge policy is keyed by
immutable GitHub repository ID, defaults to `human`, and changes through an
explicitly authorized compare-and-set operation. Settings displays it read-only.
Defaults/examples remain portable and disabled; private credentials stay outside
public source.

Do not infer milestones or budgets from ambiguous earlier state. The setup
supervisor and the factory CLI share one lock and a persistent activation marker.
A new install must not share repositories with another independent controller root.

After observing the assigned PR as merged, the supervisor closes each current
member issue as completed and reconciles its final pipeline label. Failed or unknown API writes
are retried as reconciliation; they do not undo an observed merge or spend a failure.
