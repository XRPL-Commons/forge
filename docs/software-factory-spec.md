# Software factory specification

Status: design draft. The approval and candidate-invalidation contract was agreed
on 2026-09-10. Builder dispatch, context, ownership, publication, and draft-versus-
submission decisions, shared worker scheduling, and the simplified failure budget
were added on 2026-09-11.
Items marked proposed or unresolved remain under discussion.
This document does not describe the currently deployed implementation.

## Objective

Move GitHub issues to verified merged code deliberately and reliably. The factory
maintains a durable issue history, uses that history and current GitHub facts to
select the next step, and supervises the agents that perform the work.

GitHub is authoritative for issue requirements, commits, PR contents, reviews,
repository protections, and merge facts. The factory ledger is authoritative for
assignments, attempts, accepted results, failure budgets, resource reservations, and
orchestration decisions. Pipeline labels display reconciled state; a label alone
does not establish that a required step succeeded.

Starting work creates an attempt. Only accepting its verified output advances the
issue to the corresponding milestone. A completed execution does not prove issue
completion, and releasing a worker does not authorize merging.

## Builder dispatch and publication contract

### Supervisor cycle and admission

The supervisor first cycles through existing workers, reconciles their updates,
addresses pending lifecycle conditions, and frees slots whose release requirements
are satisfied. It must preserve running work and record unresolved blockers.

When a builder slot is available, the supervisor starts triage: list eligible
GitHub issues labeled `ready`, select the next issue, and assign it to a builder.
Issue ordering and the triage mechanism remain to be specified.

The `ready` label is the discovery criterion. Dispatch must also respect the
factory's existing assignment ownership, holds, dependency constraints, and failed
retry budgets. A label must not dispatch a second builder for an issue already
owned by an active attempt, or silently reset an exhausted retry budget.

Record the assignment and reserve its slot before starting the builder. Starting
the attempt does not establish `built`; the UI may show the activity as Building.

### Assignment note and context access

The builder receives a compact supervisor note identifying the issue to process
and the lifecycle information relevant to its assignment. The complete issue,
comment history, repository context, and previous transcripts do not need to be
copied into the initial prompt.

The builder may read relevant information from the repository and GitHub,
including issue comments, candidate PR discussions, and previous findings. A tool
for retrieving additional internal lifecycle information on demand is a proposed
interface; its exact contract remains to be designed.

The note must identify the assigned work unambiguously and tell the builder where
to obtain the relevant context. Its detailed fields are part of the assignment
data contract still to be specified.

### Builder and supervisor write authority

The builder may modify its assigned code branch, work with the assigned issue
(including commenting), and create or update its candidate PR. It may publish
findings and questions to the supervisor. Whether that reporting uses output text,
a dedicated tool, or both remains unresolved.

The supervisor owns internal factory state and pipeline labels. Builder reports
are inputs to reconciliation, not direct internal state transitions. Broader issue
edits, such as changing acceptance criteria or closing the issue, require an
explicit write-authority policy; permission to comment does not settle that policy.

### What establishes built

`built` means a published candidate PR linked to the issue and its builder has
been explicitly submitted for review and verified by the supervisor. It
establishes a recoverable implementation candidate, not a passing correctness
review or security audit.

The publication contract must distinguish the assigned build attempt from the
shared builder role identity. PR existence alone is insufficient if the supervisor
cannot establish that association.

The builder may open and update a draft PR early to preserve work. A draft does
not establish `built` or authorize a Review assignment. When the builder finishes,
it explicitly submits the candidate for review; the supervisor verifies that
submission against the publication contract before advancing the issue.

Submission uses the PR's draft/ready status and the metadata below, so it remains
recoverable after an agent crash. Required builder validation and its reporting
format must still be specified separately; this milestone does not by itself
require every check to have passed.

### Recoverable publication by default

Workers must publish candidate PRs according to an explicit factory data contract
so the supervisor can recover them even if the agent stops before reporting.
Recovery must not depend solely on a conversational issue link, a branch name
heuristic, or finding a PR with a similar title.

Use one small structured block in the PR body. No separate submission comment or
custom publication tool is required. The block contains:

- Contract version.
- Assigned issue number (in the PR's repository).
- Build-attempt ID supplied by the supervisor.
- Submitted head commit SHA, unset while the build is unfinished.

GitHub supplies the repository, PR, author, branch, current head, and draft status;
do not duplicate those facts in the block. To submit, the builder pushes its final
changes, records the submitted head, and marks the PR ready for review last.

The supervisor accepts an open, non-draft candidate only when its issue, attempt,
and publisher match the recorded assignment and its submitted head matches the
current GitHub head. Editable metadata is a claim to validate, not proof of
ownership by itself. Missing metadata or duplicate or conflicting candidates
require reconciliation; the supervisor must not select one by guesswork.

Rework normally reuses the same PR: return it to draft, update the attempt ID,
clear the submitted head, then submit again when finished. A subsequent push does
not by itself establish a new completed build. The supervisor retains previous
submissions and decisions in its durable history; the PR block describes only the
current submission. Search/reconciliation mechanics and exceptional candidate
replacement rules remain to be specified.

### Baseline failure and retry rule

Use one failure counter per issue across Build, Review, Audit, and Merge. Three
failed attempts total enter `failed` and stop automatic dispatch. This replaces
the earlier budget of three rejected build cycles and separate execution retries.
Successful steps do not reset the counter. A failure consumes it once, even if the
supervisor observes the same result repeatedly or cleanup also reports a problem.

On a confirmed failure, retain the reason and findings, abandon the attempt's
unfinished work, stop its owned execution, and release its resources only after
termination is verified. No elaborate salvage of partial edits is required. Retry
from the last verified milestone while budget remains. A code rejection or merge
conflict routes to Build with findings; an execution failure retries the affected
step. Earlier verified publications remain available as input, but rejected or
superseded evidence cannot authorize advancement.

An unknown outcome is not yet a confirmed failure. Check for an already published
result or completed merge before retrying; a missing terminal alone is not proof
of failure. Waiting for capacity, provider availability, or human approval does
not create failed attempts. Explicit user cancellation does not consume the
budget. Bounded execution deadlines and the observation protocol still need to be
specified so that an attempt cannot remain unknown indefinitely.

At the third failure, record `failed`, retain the history, and require an explicit
human retry decision to grant a new budget. Relabeling an issue does not reset it.

### Failure investigation

Every confirmed failure must have a durable investigation record, even when the
work cannot be recovered or the issue has exhausted its budget. Record what
happened, supporting evidence, the known cause or remaining uncertainty, any
verified recoverable publication, and the recommended next action. An unsuccessful
investigation must still record what was checked and why it could not conclude.

Findings must drive subsequent attempts. Before retrying, the supervisor compares
the investigation with earlier failures for the issue and records the retry plan.
The next worker's assignment note includes the relevant findings, previous
unsuccessful approaches, and what this attempt should do differently.

If two attempts fail with the same underlying pattern, an unchanged third attempt
is forbidden. The investigation must recommend a concrete change that addresses
the evidence: for example, a different implementation approach or a corrected
execution setup. A fresh session or a reworded instruction alone is insufficient.
Compare causes and evidence, not just identical error text; record uncertainty
instead of inventing a diagnosis. If no justified alternative can be identified,
hold for human guidance before spending the remaining attempt.

If the changed third attempt fails, enter `failed` and request human intervention.
The escalation record summarizes the failures, the approaches tried, why the
change did not resolve the problem (or what remains unknown), and the decision or
help needed. Any third failed attempt exhausts the budget, even when the causes
differ. Human escalation is recorded visibly for the operator; outbound notification
channels remain a separate configuration decision. Routing a later investigation
to a more capable model is a future extension, not an automatic extra retry.

Proposed implementation: one shared recovery-worker slot, separate from Build,
Review, and Audit. The supervisor records the failure and captures the attempt's
logs and execution identity before cleanup, then queues one bounded investigation
for that failed attempt. It continues supervising other issues while the affected
issue waits for the investigation. Investigation also runs after a third failure;
it does not grant another pipeline attempt.

The recovery worker reads the captured evidence, issue history, and GitHub facts,
then returns structured findings. For the first version it diagnoses and identifies
recoverable publications; it does not edit code, reset budgets, change labels or
internal state, merge, or launch replacements. The supervisor verifies its findings,
records the disposition, and retries or holds the issue according to the budget.
Unknown external effects or execution ownership must be resolved before replacement
work starts. If investigation proves the original publication succeeded, the
supervisor records a correction and removes that attempt's provisional failure
charge exactly once.

Allow at most one investigation execution per failed attempt. If it crashes or
times out, record the incomplete investigation and its available evidence without
launching another recovery worker for it. Investigation does not consume another
issue failure; the original failed attempt is counted once. Its timeout and result
format remain to be specified with the shared worker lifecycle.

### Publication failures and recovery

| Situation | Required behavior |
| --- | --- |
| Draft PR published or updated | Preserve the candidate; keep the issue in the build stage without scheduling Review |
| Builder crashes with only a draft or no verified submission present | After confirming the failure, abandon unfinished work and retry Build within the issue budget; do not infer readiness for Review from PR existence |
| Candidate explicitly submitted for review and builder reports it | Verify the submission and publication contract, record `built`, and make the issue eligible for Review |
| Candidate explicitly submitted but builder crashes before reporting | Recover the submission from the PR contract and assignment record; accept it after verification without requiring another builder report |
| Builder reports success but publication fails | Do not advance the GitHub pipeline or internal milestone; record one failed attempt and retry within the issue budget |
| Publication outcome is temporarily unknown | Reconcile before retrying publication or starting replacement work |
| Candidate cannot be reliably matched to the assignment | Record the attempt as failed and abandon its work; do not guess or delete an ambiguously owned PR |
| The same publication is observed repeatedly | Accept it once; do not duplicate transitions or Review assignments |

Unrecoverable work may be discarded from the factory's active attempt. Physical
cleanup needs an ownership and retention policy; ambiguous association must not
authorize deleting another actor's work. No deletion is performed as part of this
design discussion.

Publication failures, execution failures, and code rejections retain distinct
reasons in the history but consume the same issue failure budget.

## Deterministic worker runner

The runner's primary contract is recoverability. Crashes are expected operational
events. A failed job may lose all unfinished work, but resetting its execution
environment must return runner capacity to a clean, available state. Exhausting
an issue's failure budget stops that issue; it does not retire the runner or
prevent the factory from processing other issues.

Every role uses a deterministic runner around its coding harness. The runner
persists the assignment before launch, owns an identifiable execution scope,
captures output, reports status and heartbeats, enforces deadlines, and records a
terminal execution result. The supervisor owns issue state; the runner owns its
execution record. A terminal is an inspection surface, not the execution ledger.
Runner health, harness activity, and verified task success are distinct facts.

Runners are role-agnostic. Build, Review, Audit, and Recovery are values of the
assignment's role parameter, not separate runner implementations. Role
configuration supplies the instructions, permitted capabilities, and expected
result contract. The runner uses the same launch, observation, termination,
cleanup, and reporting operations for every role; it does not decide pipeline
transitions. The supervisor selects the role and applies role-specific acceptance
and routing rules to the returned evidence.

### Reset and return to service

A runner has a reusable capacity identity; every job receives a new execution
identity and disposable execution environment. Reset ends the old execution and
returns the capacity to service. It never resumes an abandoned job implicitly.

Reset must be executable by an external lifecycle controller even when the runner
has crashed, hangs, or has lost its local files. The supervisor retains enough
ownership information outside the disposable environment to identify and destroy
the job's resources. Reset uses deterministic lifecycle operations, not a recovery
agent's diagnosis, and does not wait for that diagnosis to finish.

The reset contract is:

1. Record that the old execution is being abandoned and invalidate its authority
   to launch work or release resources belonging to a replacement.
2. Capture available diagnostic evidence without making recovery of logs or edits
   a prerequisite for reset. Record explicitly when evidence was lost.
3. Terminate the entire owned execution scope. If graceful shutdown fails, force
   teardown of that scope from outside the runner.
4. Discard the owned disposable environment, verify its resources are released,
   and provision a clean environment. A known contaminated workspace is never
   reused merely because cleanup was attempted.
5. Verify readiness and mark the runner available. A later job uses a new
   execution identity; stale messages cannot advance it or terminate it.

Reset is safe to repeat and has bounded operations. Its state survives controller
restarts. Missing worker-local state must not make an execution unmanageable.
The selected containment mechanism must support this external teardown contract;
ordinary harness or runner crashes must not leave capacity permanently blocked.

The supervisor separately reconciles any external publication, records a failed
attempt when appropriate, and schedules its mandatory investigation. A completed
reset restores capacity even when that issue is waiting for investigation or has
entered `failed`. Lost diagnostics produce an incomplete findings record, not an
endless cleanup loop. Reset itself does not charge the issue another failure.

If the host or resource manager cannot verify teardown, mark the affected capacity
unavailable and escalate the infrastructure fault. Do not claim that resources
were freed or reuse them while the old execution may remain alive. Other healthy
capacity continues processing work.

### Termination and cleanup contract

Termination and cleanup are required on every exit path: normal completion,
failure, timeout, explicit stop, and recovery after a runner or supervisor crash.
An agent's final message or the disappearance of its terminal does not establish
that its processes have stopped or that its slot is reusable.

Use the same shutdown sequence for every role:

1. Persist the intent to stop or finalize. Prevent further launches under that
   execution identity and retain exclusive ownership until release is verified.
2. Preserve available results and logs. Request graceful shutdown of the owned
   harness and its subprocesses, allowing a bounded grace period.
3. If processes remain, force termination within the owned execution scope. Verify
   the scope is empty, including subprocesses left behind after the harness exits.
4. Finalize the durable execution record, including exit reason, available result,
   logs, and termination evidence. A forced stop cannot by itself establish task
   failure or success; the supervisor reconciles any external publication.
5. Clean up the execution's terminal, temporary files, and disposable checkout.
   Retain lifecycle history and diagnostic evidence outside the disposable
   workspace. Do not delete a published PR or branch as an implicit cleanup step.
6. Mark cleanup complete and release the reservation. Only then may that slot and
   workspace be assigned again.

The runner must contain subprocesses in an execution scope whose ownership can be
verified after a restart. Killing only the visible terminal or the harness's main
PID is insufficient. The concrete operating-system containment mechanism remains
an implementation choice and must cover background or detached subprocesses.

Stop and cleanup operations must be safe to repeat after interruption. They target
the recorded execution identity, not a reused PID, terminal name, or workspace
path alone. An old runner must not terminate a replacement execution or release
its reservation. Replacement work cannot begin while the old execution may still
write to the same resources.

If normal termination or cleanup cannot be verified, use the external reset path
above to tear down and replace the disposable environment. Keep affected resources
reserved until teardown is verified. A cleanup problem does not undo a verified
issue milestone or count the same failed attempt twice. On restart, the supervisor
reconciles incomplete shutdown/reset records before permitting reuse; it does not
assume that a missing runner cleaned up.

Execution deadlines, graceful-stop duration, evidence retention, and the exact
runner status/result format remain to be specified before implementation.

## Review, audit, and shared supervisor scheduling

Each supervisor cycle reconciles existing workers and GitHub evidence, records
accepted results in internal state, and cleans up finished execution resources
before allocating available Build, Review, and Audit slots. Cleanup releases only
resources verified safe to release; an unresolved worker does not prevent dispatch
to other available slots. No role may bypass assignment ownership or reservation.

Only the supervisor updates internal pipeline state. Agent reports and published
GitHub results are evidence for its decisions. It records each accepted result and
transition once, then assigns eligible work when the corresponding slot is free.

Reviewers and auditors receive the issue, candidate PR, exact commit, and a compact
lifecycle note. They publish a verdict and actionable findings tied to that
candidate. They report findings; builders make code corrections, which require
fresh Review and Audit. Publication and verification of a verdict must precede
acceptance of the result.

| Accepted outcome | Supervisor action |
| --- | --- |
| Review passes | Make the candidate eligible for Audit |
| Audit passes | Proceed toward merge, or wait for required human approval |
| Review or Audit rejects the implementation | Count one failed attempt and return findings to Build if budget remains; the third failure enters `failed` |
| Review or Audit cannot finish | Count the confirmed failed attempt and retry that step if budget remains; record the execution reason separately from a code rejection |

Review and Audit run sequentially for each candidate: Review must pass before
Audit is eligible. Different issues can occupy different pipeline stages at the
same time.

Build, Review, and Audit have independently configurable slot limits. Proposed
starting allocation: four builders, two reviewers, and one auditor, subject to
shared machine and provider capacity. These are concurrency limits, not targets
that must always be filled. The supervisor dispatches only eligible work within
both the role limit and shared resource limits. Queue lengths and observed stage
durations guide later adjustments; automatic capacity tuning is out of scope for
the first version.

## Approval and candidate-invalidation contract

### Technical audit and human authorization

The auditor reports two separate dimensions:

- Technical verdict: pass, fail, or unable to conclude.
- Human approval requirement: required or not required, with a reason.

Repository policy can require human approval independently of the auditor. The
auditor can escalate additional cases, but cannot waive a policy requirement.
Unknown approval requirements block merge eligibility.

| Audit result | Human requirement | Factory behavior |
| --- | --- | --- |
| Pass | Not required | Proceed toward merge when the other gates pass |
| Pass | Required | Enter `approval_needed` and wait for an authorized human |
| Fail | Either | Return the findings to Build, subject to the issue failure budget |
| Unable to conclude | Either or unknown | Record an explicit blocker and its remediation path |

Human approval supplements a passing audit. It does not substitute for an
unfinished or failed audit. Policy exceptions require an explicit, separately
recorded decision; an ordinary approving review is not a policy exception.

### GitHub enforcement

Require these checks on the protected merge target:

| Required check | Meaning | Expected producer |
| --- | --- | --- |
| `forge/review` | Correctness review passed for the current candidate | Reviewer identity |
| `forge/audit` | Security audit passed for the current candidate | Auditor identity |
| `forge/merge-policy` | All additional merge requirements are satisfied, including conditional human approval | Controller identity |

Bind each check to its expected GitHub App. Readable GitHub reviews carry findings
and decisions alongside these checks. The controller must honor all repository
protections and must not use a bypass to complete routine factory work.

A rule requiring two approvals alone is insufficient: the count does not establish
that the correctness reviewer and security auditor each passed, or that an
additional authorized human approved an escalated candidate. Native review-count
rules may supplement the named checks. Whether the chosen App identities' reviews
count toward those rules must be verified before depending on that behavior.

### Human-approval wait

When an audit passes and human approval is required:

1. Record the accepted audit result and escalation reason for the candidate.
2. Complete `forge/audit` successfully. Keep `forge/merge-policy` pending, with the
   required decision and eligible reviewer role explained on the PR.
3. Enter `approval_needed`. End the audit attempt and release its execution
   resources after the normal preservation and termination checks.
4. Observe GitHub for a native approving review from an authorized human. The PR
   author and factory automation identities do not satisfy the human requirement.
5. Verify the approval covers the current candidate and that no blocking review,
   unresolved requirement, or other protection prevents merging.
6. Complete `forge/merge-policy` successfully and proceed to the merge action.

Waiting for approval does not consume a worker slot, launch another auditor, or
consume the failure budget. A duplicate observation of the same approval must
not create a duplicate transition or merge operation.

### Code changes invalidate prior authorization

Enable GitHub's **Dismiss stale pull request approvals when new commits are pushed**
protection. Use GitHub's native approval state rather than inventing a parallel
internal approval mechanism.

The factory additionally binds every accepted review, audit, and approval decision
to the candidate evaluated: repository, issue, PR, head revision, evaluated base,
attempt, and relevant requirement/policy versions.

When the PR head changes:

1. Retain earlier results as history; they do not authorize the new candidate.
2. Require fresh review and audit results for the new head. A builder submission
   must first satisfy the publication contract before entering Review; separate
   builder validation requirements remain to be specified.
3. Leave `approval_needed` for revalidation. Do not keep waiting for approval of
   code that has changed, and do not mark the change as a failed review by itself.
4. Keep the new candidate's merge-policy gate unsatisfied until its requirements
   have been reevaluated. Do not copy successful checks from the prior head.
5. Request fresh human approval if the revised candidate still requires it.

Changes to the evaluated base, requirements, or policy must also trigger
reconciliation of affected evidence. The final candidate-identity contract must
specify this precisely, including merge-queue candidates if a merge queue is used.

Approval dismissal, revocation, or a qualifying request for changes must trigger
reevaluation even if the head does not change. The controller must read current
GitHub evidence before merging; a remembered internal approval is insufficient.

### Example

```text
Candidate A: review passes -> audit passes, human required -> approval_needed
New code creates candidate B
Candidate A results remain historical; B is not authorized to merge
Candidate B: publication verified -> review -> audit
  audit fails                 -> Build with findings
  audit passes, human needed  -> fresh human approval -> merge eligibility
  audit passes, no human need -> merge eligibility
```

The shared three-failure limit applies throughout the pipeline. Code changes,
approval waits, and worker cleanup do not independently consume this budget;
confirmed execution failures do. Retrying after exhaustion requires an explicit
human decision and retains the previous history.

## Acceptance scenarios for this contract

| Scenario | Required outcome |
| --- | --- |
| Review and audit pass; no human requirement | Merge eligible only when every other repository gate passes |
| Review and audit pass; human required but absent | Merge blocked; no audit worker retained or relaunched |
| Audit fails and a human approves | Merge remains blocked; technical findings still require resolution |
| A factory bot approves an escalated PR | Human requirement remains unsatisfied |
| An authorized human approves the current passing candidate | Controller can satisfy merge policy after current-state verification |
| New head appears while waiting for human approval | Return through validation; old results cannot authorize the new head |
| New head appears after human approval | Native stale-review rules apply; fresh factory checks and applicable human approval are required |
| Approval is dismissed or changes are requested without a code change | Reevaluate and block merge as required |
| An old attempt reports success after a replacement candidate exists | Preserve the report as history; do not advance the current candidate |
| The controller restarts during an approval wait | Recover the wait from the ledger and current GitHub facts without duplicating work |
| A label is changed to imply approval | No gate is satisfied without the required evidence |
| A failed attempt is observed again after a supervisor restart | Do not count the failure twice or dispatch a duplicate replacement |
| Three distinct attempts fail across different stages | Enter `failed` and stop automatic dispatch, even if some intermediate steps passed |
| Two attempts fail with the same underlying pattern | Require an evidence-based change in approach before the third attempt and pass prior findings to its worker |
| Two matching failures have no justified alternative approach | Hold for human guidance; do not spend the third attempt repeating the same approach |
| The changed third attempt also fails | Enter `failed` and present the human with the evidence, attempted changes, and help needed |
| An agent crashes after publishing a verifiable successful result | Accept the verified result; do not count a failure just because the execution disappeared |
| Harness exits but a background subprocess remains | Keep the reservation, terminate the remaining owned execution, and verify termination before cleanup and release |
| Runner crashes during shutdown | Resume shutdown from its durable record without duplicating work or releasing resources prematurely |
| An old cleanup request arrives after a slot has been reassigned | Reject the stale execution identity; leave the replacement's processes and reservation intact |
| Cleanup fails after a verified successful publication | Retain the milestone, block reuse of affected resources, and record the cleanup problem |
| Runner crashes and all worker-local state is lost | Reset using externally retained ownership data, record missing evidence, and restore clean capacity without requiring work salvage |
| An issue exhausts its budget after a runner crash | Stop dispatching that issue, investigate its failure, and return the reset runner to service for other eligible issues |
| Graceful cleanup hangs | Use bounded external teardown and reprovisioning; mark available only after verified release and readiness |

## Configuration and GitHub onboarding

Configuration files are authoritative for operator intent and are editable by
humans, agents, and the Settings UI. UI saves validate the full document and reject
stale revisions. Credentials remain outside source/configuration files; records
contain credential references only. Issue history is separate durable state.

Humans create the role GitHub Apps and select repositories during installation.
Forge does not require a standing administrator credential to provision access.
Startup, periodic, and manual sync read each installation's repositories and grants.
Every required role needs appropriate access to the same repository identity;
permissions differ by role. Missing access and failed observations are distinct.

Discovery never enables automation. Operators explicitly enable repositories in
configuration after all required access is verified. Initial issue/PR reconciliation
and execution setup must complete before dispatch. New, removed, renamed, suspended,
or inaccessible repositories cannot silently inherit authorization. Access loss
pauses new dispatch while preserving history; a failed sync records access as
unknown and retains the previous inventory for diagnosis only.

The first settings implementation is described in `docs/github-settings.md` in the
Forge repository. It provides setup, access sync, activation, and admission guards;
per-role execution credentials and the deterministic runner remain integration
work. Configuration readiness is not evidence of execution isolation or merge
policy readiness.

## Remaining design work

Before implementation, complete the issue transition table, including expected
agent output, allowed GitHub writes, interruption recovery, timeout behavior, and
remediation for each transition. Translate the runner contract into explicit
attempt, execution, terminal-session, and workspace-lease states and operations.

Resolve these approval-specific details:

- Authorized human identities/teams and how policy exceptions are recorded.
- Exact GitHub App permissions, native review eligibility, and required-check
  producer configuration.
- How same-head approval/policy changes invalidate a previously successful check,
  including reconciliation delays and races with merge requests.
- Whether merges are restricted to a controller-operated path, and how native
  repository rules protect other permitted merge paths.
- Candidate identity and required revalidation when the base or policy changes.

## GitHub references

- [Protected branches and stale approval dismissal](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)
- [Approving PRs and review eligibility](https://docs.github.com/en/pull-requests/how-tos/review-pull-requests/approving-a-pull-request-with-required-reviews)
- [Required-check behavior and expected App sources](https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/troubleshooting-required-status-checks)
