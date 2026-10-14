# Five-run implementation plan

Source: [the 14 October 2026 run plan in #269](https://github.com/XRPL-Commons/forge/issues/269#issuecomment-6162894077).
This supersedes the earlier delivery order. The architecture and evidence
requirements in [target-architecture.md](target-architecture.md) still apply.

Planning baseline: `main` at `ccba2ce`, schema 035, no open PRs when checked on
14 October. PRs #253 and #270 are merged. The last production preflight on
13 October reported schema 033 and workers awaiting cleanup; this is historical
evidence, not a fresh deployment check. No implementation or deployment is
performed by this plan.

## Working agreement

Each run has one integration branch, one combined PR, one full CI gate, one
independent review, one merge, one deployment and one live acceptance record.
Lanes use separate worktrees and run focused tests while developing. There are
no lane PRs or repeated full suites after individual issues. A failed combined
gate is fixed and rerun; “one CI” is not permission to merge a failed revision.

Freeze shared interfaces before parallel edits. Assign a single owner to every
shared file, including tests and UI types. Other lanes supply changes through that
owner. Reserved migration numbers do not by themselves prevent conflicting
schema changes. Migrations must apply in sequence from the deployed schema.

With four agent slots, Run 1 uses one integrator and three implementation agents.
Run 2's fourth lane uses a second wave as a slot becomes available. Independent
review begins after integration, with an agent that did not author the reviewed
code. Implementation of the next run may proceed during observation, but the
release remains under acceptance until its live checks pass.

## Run 1 — reliability (M0)

Ship #270 with this run. It already supplies #260, a trusted startup-fault slice
of #262 and the unsuccessful-build candidate guard from #255.

| Lane | Scope | Primary ownership | Migration |
| --- | --- | --- | --- |
| A | #261 persisted declines, stall detection, build stale-base repair; #264 backoff and bounded uncertainty | Dispatch, merge and effect controllers; effects store; supervisor scheduling; status projection | 036 |
| B | Remaining #262 exit envelope, provider errors, timeout/OOM policy, deduplicated Report to Forge draft | Harness and adapters, runner, job lifecycle, execution classification | 037 |
| C | #263 explicit and automatic re-test of worker claims | Runtime requirements, requirements controller, operator Recheck, wait controller | 038 |

### Shared contracts before lanes start

- **Scheduling:** one durable next-action representation, with exact work scope,
  reason, due time and responsible component. Admission or a changed assignment
  invalidates obsolete schedules. A future schedule counts as owned work; it is
  not a stall merely because it is not due yet.
- **Execution:** a versioned envelope bound to the actual job and trusted runtime
  observations. Preserve #270's pre-agent proof boundary. Missing or malformed
  output must not let a worker forge a trusted timeout, OOM or provider error.
  Keep missing-envelope failures distinguishable from a valid `cannot_complete`
  result; persist the evidence needed for deterministic classification.
- **Claims:** typed claim identity plus relevant release, repository/rules,
  environment and login revisions. Store safe identities/digests, never credentials.
  Worker-claimed `github_permission` currently lives inside an environment gate;
  preserve that distinction when releasing only the selected obligations.
- **Charging:** one durable disposition per physical attempt. Forge faults and
  provider cooldowns are uncharged; timeout and unchanged repeated claims charge
  once across replay; OOM follows bounded deterministic escalation. Exhausted
  resources produce an owned wait, not an infinite retry.
- **Recovery:** Run 1 records the need for recovery and uses the existing path.
  The new steward is Run 3; no Run 1 acceptance claim implies it already exists.
- **Defaults:** pin stall duration, uncertainty duration, backoff and attempt caps
  in configuration and clock-driven tests before implementation. These are routine
  implementation choices, not additional operator approvals.

The integrator owns edits to `result_controller.py` (A supplies pending-return
changes; B supplies charging changes), shared lifecycle/schema registration,
runtime wiring and shared tests. Lane A owns the status/UI contract; B and C
supply their display and action requirements. Lane C integrates B's new wait and
retry cases into `wait_controller.py`. B implements fault-report drafts and
deduplication; publication remains an explicit operator action.

### Required behavior

- Every dispatch/result/merge decline leaves a durable schedule or an owned gate.
  Cleanup, captured results, pending effects, maintenance and bounded scheduler
  continuation count as ownership. Preserve fairness and independent child work.
- “Queued” means a real schedule, with reason and next try time. An unowned unit
  becomes visibly stalled after the configured bound, without an issue charge.
- Recheck and relevant changes release only worker-claim obligations. Independent
  pauses, labels, dependencies, human verification and runtime-selection gates
  retain their own authority. Retry bounds survive restarts.
- Backoff applies at both effect selection and claim/send admission. A fresh read
  after the last uncertain send can establish an exact merge, a closed-unmerged
  PR or a changed head even after multiple sends. Preserve the existing single-send
  guard for HTTP-refusal evidence; it is a different proof.
- Sustained observed conflict returns work for rebuilding. Unknown mergeability
  stays uncertain and eventually escalates; elapsed time never proves conflict
  or success. Future publisher effects reuse this contract in Run 2.
- Fault reports are scrubbed drafts, deduplicated by safe signature. Deploy retries
  use relevant qualified release/image evidence, with no repeated retry for the
  same identity. Preserve the scoped qualified-image exit already in #270.
- Source inspection found that ordinary build publication is currently performed
  by the agent, not by an image-owned publisher. Move #265's Codex shell credential
  removal into Run 2 with the publisher; removing it independently would break
  successful builds. Canaries must test the boundary when that replacement ships.

### Combined acceptance

One regression batch covers this week's stale-base, signing-wait, harness-crash
and stranded-request incidents, plus restart/replay, independent holds, grouped
work, multiple uncertain sends, provider cooldowns, timeout charging and OOM
escalation. Test migrations 034–038 from the deployed schema, canonical status
parity and the visible operator actions. Then run full CI and independent review
on the combined diff; fix any findings before merge.

Deploy through the supported CLI. Because this run changes schema, use
`forge migrate upgrade` from the exact release, with a read-only plan first and
then the resumable apply command. It drains workers, migrates and invokes the
normal updater. A bare `bin/forge update` is not a substitute for that migration.
The integrator owns completion of drain, service restoration and image qualification.

Record release/schema/image identities and the start of live observation. For
24 hours, every nonterminal unit must have **a live job, a durable next action,
or an owned gate**, within the defined stall-detection bound. Include cleanup and
effect ownership in the audit. A discovered orphan must become visible and have
an exit; an empty board alone does not prove the incident scenarios. Record gaps
or failures honestly and repeat the affected acceptance after correction.

## Run 2 — worker/Forge boundary

| Lane | Scope |
| --- | --- |
| A | #255 supervisor publisher: verified bundles, App commit, read-back, PR/marker, Observed by Forge facts |
| B | #259 mirror and immutable notes for every role; remove worker GitHub credentials, `gh` and GitHub egress |
| C | #265 bounded work volume, worker network isolation and read-only registry proxy |
| D | #271 B1–B3: CLI version/help/re-login, bypass-only repair, interview/preflight and resumable apply |

Both Run 2 decisions were supplied by the operator on 14 October:

- Forge serves a read-only Git mirror. Sandbox fetches, clones, submodules and
  package-manager Git dependencies use it without receiving credentials. Fetch
  uncached repositories on demand when policy allows them: declared project
  dependencies and repositories in organizations where the Forge App is
  installed are allowed by default. A per-project deny list overrides those
  defaults for sensitive repositories. Refuse other repositories and expose the
  refusal as a typed claim for the steward. Record every fetch against its job.
  The host fetch still requires actual GitHub access; policy is not a grant of
  credentials or permission. Pin dependency declarations to the assignment so a
  worker cannot expand access by rewriting its manifest during execution.
- The signing test is approved in a throwaway XRPL-Commons repository, temporarily
  added to the Controller installation. Create a Git Data API commit without a
  custom author/committer, read back verification and prove the organization
  signing rule accepts the ref update. Test workflow changes with and without
  the relevant granted permission; do not silently expand App permissions.
  Delete the scratch repository afterward. If REST commits are unsigned, use
  `createCommitOnBranch` and refuse unsupported file-mode changes explicitly.

Run this proof at the start of Run 2, before choosing the publisher implementation.
Publisher receipts and mirror/note contracts are shared interfaces between lanes
A and B. Signing proof and exact-tree support are separate acceptance checks.

Acceptance: thegame publishes under its signing ruleset, every role passes on a
canary repository, and an egress test proves workers have no GitHub route. Any
approved private-dependency exception is explicitly excluded from that proof.

## Run 3 — steward v1

Three lanes: core dossier/decision v1, guards, autonomy and durable repository
notes (#258/#217); recovery, stall and brief execution modes (#258/#266); and one
shared UI choices component. Freeze typed decisions before executor and UI work.

Acceptance: the captured stuck cases resolve automatically or through one
operator choice, while budgets, exact revision guards and independent holds
remain enforced. Forge faults remain deterministic core handling.

## Run 4 — consolidation and identity

Three lanes: #267 triage/conversation steward modes, four gate classes, fewer
states, one outbox and result v2; remaining #266 typed dependencies, environments
and ownership; #259 one-App verdict checks/bypass and trusted Tailscale identity.
One schema owner integrates state and contract changes. UI/proxy files also need
explicit ownership across the first and third lanes.

Before starting, decide whether `forge/merge-policy` applies to all PRs or only
Forge branches. Preserve stronger repository policies and an achievable path for
human PRs. Acceptance includes resumable identity/App migration, independent
logical Review/Audit, spoofed identity-header rejection and agent revocation.

## Run 5 — install and cleanup

Three lanes: #271 B4 single-App consent, smoke merge and handover; #268 legacy JSON
runtime removal while retaining the importer; #259 provider credential isolation
using proven access-only support or a host-side model proxy.

Resolve old wording in #271 against the one-App target: no new role-App wizard,
no signing bypass as a substitute for signed publication, no worker-token
revocation acceptance after tokens are removed. Its touch-count figures also
disagree; use its explicit done-when threshold of at most 12 as the proposed
acceptance target, and measure rather than claim it in advance.

Acceptance: the supported CLI installs a fresh instance, a real smoke issue
reaches a GitHub-confirmed merge, interruption/resume converges, and the report
measures time to first merge. Include Anvil's supported upgrade and a human-approval
hold. Confirm all known installs use SQLite before removing runtime selection of
JSON. Provider capability proofs precede credential migration.

## Evidence and issue tracking

Run 1 merged as [PR #272](https://github.com/XRPL-Commons/forge/pull/272)
and deployed on 14 October. See [the acceptance record](run-1-acceptance.md).
Its 24-hour observation remains open while Run 2 is developed.

New cleanup issues #273 and #275 belong after the core replacements in this
plan. Check callers before removing each proposed item. In particular, keep
the current repository-rule repair until #271 supplies its replacement, and
keep the observer deferred under the operator's earlier #111 decision. The
proposal in #273 does not itself reverse that decision. Group compatible docs,
dead-code and UI removals with Run 5; do not mix them into the security boundary
change or reset Run 1's observation with an unrelated deployment.

Keep broad parent issues open until their complete acceptance is met. Each run's
acceptance note records the tested revision, CI/review, deployment identity,
live outcomes and remaining scope. Prepare the note with the run; publish it to
#269 when carrying out the approved reporting step. #111/#98/#5 remain deferred
until the core is qualified.
