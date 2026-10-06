# State and units of work

Status: **architecture and lifecycle behavior approved 2026-10-05**, phase 0 of [#209](https://github.com/XRPL-Commons/forge/issues/209).
This consolidates the initial PR #210 design and a parallel lifecycle/storage review.
It includes the shared stage and transition-event work from superseded
[#168](https://github.com/XRPL-Commons/forge/issues/168) and the batch requirements
of [#207](https://github.com/XRPL-Commons/forge/issues/207).

## Summary and review scope

Move mutable controller state into one private SQLite database. Model each delivery
as a unit containing one or more issues, with immutable candidates, scoped gates,
row-level transactions and one shared lifecycle projection for API, UI and CLI.
Migration and recovery run through the CLI; personal GitHub credentials stay local.
Time to first merge remains the product metric.

The design preserves parallel part builds, a shared three-failure budget, and
operator approval of batch proposals. It distinguishes an operator stopping work
from confirmed GitHub closure and keeps cleanup ownership until termination is
verified. The migration protocol covers all writers and moves each state domain
to SQLite once. There is no JSON switch-back; the source copy is diagnostic only.

The operator approved the architecture and lifecycle behavior, then resolved the
seven policy choices below. Implementation follows #209's phases 0–7. The current
foundation is inactive; migration, automatic resumption, batch orchestration and
retention execution ship in their respective later phases.

The original design remains available in commit `2dd3f89`. This revision replaces
its unit-wide job exclusion with member-scoped eligibility, mutable charge flags
with immutable budget accounting, the backend flag with a guarded database path,
and artifact-first two-store rollout with an inactive schema followed by verified
cutover. These changes remove concurrency, recovery and migration ambiguities.

## Lifecycle audit and proposed shared model

This section defines the approved lifecycle architecture. Code evidence is from main `16ad85e`; references name functions so they remain useful after nearby edits.

### Legacy states and writers

`STATES` in `contracts.py` lists `ready`, `built`, `reviewed`, `audited`,
`approval_needed`, `investigating`, `hold`, `failed`, `merged` and `cancelled`.
`NEXT_ROLE` maps `ready→build`, `built→review` and `reviewed→audit`. Running is not
a state: it is `issue.active`, a pointer to a job, which can also be a `merge`
pseudo-job.

| Transition | Writer (file: function) |
| --- | --- |
| *(new)* → `ready` or `hold` | `ledger.discover_in` (`hold` if a hold label is present at discovery) |
| milestone → next milestone | `ledger.accept` via `PASS_STATE` |
| any → `investigating` or `failed` | `ledger.fail` (`failed` at 3 charged failures since `budget_start`) |
| `investigating` → `RETRY_STATE` stage or `hold` | `ledger.findings` (sets `hold` with `APPROACH_HOLD` on a repeated pattern) |
| `investigating`/`failed` → `built` | `ledger.recover_publication` (uncharges the failure) |
| `built`/`reviewed` → `approval_needed` | `ledger.request_verification` (stores `resume_state`) |
| `approval_needed` → `built`/`reviewed` | `ledger.complete_verification` |
| `audited` → `approval_needed` | `supervisor.merge` (direct dict edit; reason is free text) |
| `audited`/`approval_needed` → `ready` | `ledger.resolve_conflict` (uncharged; also fences a refused merge job, #183) |
| any → `ready`/`built`/`hold` | `ledger.invalidate`, `supervisor.pause_external_change` |
| `ready` → `ready` (+ dependency fields) | `ledger.wait_on_issues` |
| `ready` → `hold` (+ `decision_request`) | `ledger.request_decision` |
| any → `hold` (+ `environment_hold.state`) | `ledger.block_environment`; `ledger.resume_environment` restores the saved stage |
| `ready`/`built`/`reviewed` → `hold` (+ `prerequisite_hold.state`) | `ledger.pause_for_protection`; `ledger.resume_protection` restores it |
| any → `hold` | `ledger.hold` (labels, unknown outcome, host memory) |
| any → `hold` (+ `operator_hold`) | `issue_actions.decide` and `IssueActions.apply` for pause and backlog |
| `hold`/`failed`/`cancelled`/`approval_needed` → `ready` or saved stage | `ledger.retry`, **and separately** `issue_actions.decide` (queue, resume, retry, recheck, start_anyway) |
| any → `merged` | `supervisor.reconcile_current` (direct dict edit when GitHub reports the PR merged); `ledger.accept` for the merge job |
| any → `cancelled` | `ledger.cancel` (external closure, operator close, and operator Stop of an open issue; closure provenance must be checked separately) |
| `hold` → `ready` (self-healing) | `reconcile_current` regex over recovery prose (#196) and over `decision_request` text (#174) |

**Legacy holds and waits.** These are recorded in several unrelated shapes.

| Hold or wait | Recorded as | Set by | Released by | Budget |
| --- | --- | --- | --- | --- |
| Dependency | `blocked_dependencies`, `dependency_reason` (prose), `dependencies_checked_at`; `state` unchanged | `dependency_ready`, `wait_on_issues` | Fresh GitHub read shows the prerequisites closed; `start_anyway` for worker-declared ones | no |
| Ownership (#151) | `ownership_wait` = `{headline, plain}` (**prose is the state**) | `ownership_ready` | Automatic, when no claiming PR, human assignee or `wip` label remains | no |
| Label hold | `state=hold`, `reason` = one of **two** strings, `label_hold` list | `discover_in`, `reconcile_current` (×3), `dispatch` | Operator `resume` only. **Removing the label does not release it** | no |
| Operator pause or backlog | `state=hold`, `operator_hold={kind,state}` | `decide`, `apply` | `resume` or `queue` | no |
| Applying decision | `operator_sync`, `operator_request` | `decide`, `request` | `IssueActions.sync` once the labels are confirmed | no |
| Repository protection | `state=hold`, `prerequisite_hold={state}`, exact reason string | `pause_for_protection`, `migrate_protection_hold_in` | `resume_protection`, which needs the reason string to match | no |
| Environment | `state=hold`, `environment_hold={state,blocker,…}` | `block_environment` | `resume_environment` or `retry_services`/`retry_memory`, by blocker | no |
| Availability (account, GitHub) | `retry_after` plus `reason='blocked_auth'` (**a code in prose**) | `ledger.wait`, dispatch error paths | Time | no |
| Investigation | `state=investigating`, `failures[-1].investigation` | `fail` | `findings` | yes |
| Budget | `state=failed` | `fail` | Operator retry (resets `budget_start`) | — |
| Approach | `state=hold`, `reason == APPROACH_HOLD` (**string equality**) | `findings` | Operator retry, or the #196 regex | — |
| Decision | `state=hold`, `decision_request` | `request_decision` | Operator retry, or the #174 regex | no |
| Verification | `state=approval_needed`, `verification_request` | `request_verification` | `complete_verification` | no |
| Approval and merge gate (#140, #189, c060ff1) | `state=approval_needed`, `merge_gate={kind,reason}`, `reason` | `supervisor.merge` | A fresh observation passes every gate | no |
| Unknown outcome | `state=hold`, reason prose | `cycle()` after deadline + 900 s | Operator | no |

### Legacy findings and causes

The **live** column preserves the initial author’s reported observations; see
**Earlier author’s prototype report** below for their scope and limitations.

| # | Problem observed | Root cause in the model | Live |
| --- | --- | --- | --- |
| 1 | An issue can be `hold` **and** dependency-waiting **and** ownership-waiting at once. Each reader picks a different one. | Waits are independent side fields with no shared precedence. `state=hold` overloads six unrelated stop reasons. | 3 settled issues still carry `blocked_dependencies` |
| 2 | The supervisor and `presentation()` derive the same facts differently. `presentation()` checks labels before environment, decision and dependencies. `attention_kind()` returns `None` for any `blocked_dependencies` *before* it looks at a question or failure. The `hold` label is "waiting" in one and "attention" (`blocked`) in the other. Ownership, dependencies and readiness are recomputed from inventory copies, whereas the supervisor uses fresh reads. | No single function maps state to an explanation. The UI re-derives eligibility from raw fields, and the precedence is hand-coded in three places. | — |
| 3 | A stale inventory copy disagrees with the ledger. | `inventory.json` is a second copy of GitHub, refreshed only when a status API request (`legacy/forge-status.py`) calls `start_refresh()`. Supervisor observations don't update it, apart from the `github_observation` overlay on 14 issues. | Inventory was **6 h old**. 11 issues show open in the inventory but merged or cancelled in the ledger. |
| 4 | Free-text `reason` is used as a code. Examples: `reason == APPROACH_HOLD`, `LEGACY_SETUP_REASON`, `'Repository review settings need attention'`, `reason in ('blocked_auth','blocked_quota','Role credentials unavailable')`, `legacy_approval` matching on exact prose. Dispatch overwrites `reason` with `str(error)`. | There is no coded wait type. `reason` is both display text and control data. | 1 hold known only by its reason text |
| 5 | A label hold never releases when the label is removed, yet the UI says it will ("Forge leaves the issue alone until that label is removed"). | A label hold is written as `state=hold`, a terminal-ish stop. `reconcile_current` returns early for `hold` before it checks labels again. | 1 live issue in this state |
| 6 | Holds overwrite each other and lose the saved stage. `ledger.hold()` pops `environment_hold` and `prerequisite_hold`, so a resume falls back to `ready`. | The stage is stored inside each hold's dict (`{'state': …}`) instead of being kept while the hold is active. | — |
| 7 | A merge job pinned a conflicted candidate forever (#183). | `issue.active` points at both worker jobs and an external merge request. A refused merge has no completion path, so `active` stayed set. | 0 now (patched locally in `resolve_conflict` only) |
| 8 | Checks were left `in_progress` on stopped PRs (#194). `merge()` must "publish first" because a check "lost after the move would stay waiting forever". | GitHub side effects are not tied to transitions. They are best-effort calls placed around state edits, then patched by `checks_settled` bookkeeping. | — |
| 9 | Closed as completed outside Forge is recorded as `cancelled`. Activity's "completed" section mixes merged and cancelled. The Issues page splits `dropped` using `closed_as`. | `cancelled` conflates operator cancel, external completion and "not planned". `closed_as` (#174) is a patch on top of it. | 8 issues are `cancelled` with `closed_as=completed` |
| 10 | Residue fields survive transitions. | Every transition must remember to `pop()` each field. Nothing scopes a fact to a candidate or a stage. | 38 settled issues carry `merge_gate`; 17 carry `retry_after` |
| 11 | Self-healing regexes over worker prose (#174, #196). | Workers can't return a typed "waiting on #N". The build result can (`waiting_on_issues`), but recovery findings and decisions can't. | — |
| 12 | Failure data is stored three times. | `issue.failures[]` is copied into every job as `job.failure` and `job.history` at reservation. | 3.4 + 3.6 + 3.1 MB |
| 13 | **Write amplification.** Every observation, `last_observed` stamp and `provider_cooldowns` write (each cycle while a Codex login is bad, #189) rewrites 64.6 MB. | One document is the unit of locking and durability. | 1.4 s per write; jobs are 91% of the bytes |
| 14 | Logs are kept twice after a job finishes. | `job.observation.logs` (the live tail) is not dropped once `job.capture.logs` exists. | 20.2 MB of observation logs; 26.4 MB of capture logs |
| 15 | A crash during a rewrite leaves a 12 MB orphan. | `atomic_json` cleans up its temp file only in-process. | `.pending-1fh9yc66` dated Sep 29 |
| 16 | Decision idempotency scans all events (`decide()`). `event.sequence = len(events)+1`. | Events double as the request-id index, so they can never be pruned. | — |
| 17 | `Discussions.read()` takes a **write** transaction and rewrites `discussions.json` on every read. `IssueInventory.view()` scans all events and jobs per issue (O(issues × (events + jobs))). | The JSON stores have no read API and no indexes. | — |
| 18 | The retry transition is implemented twice: `Ledger.retry` and `issue_actions.decide`, plus `IssueActions.apply`. | Any caller may edit the dict, so transitions are not owned by one module. | — |

The pattern is the same throughout. Facts are stored as overlapping dict fields
with no owner, no scope and no closed vocabulary. JSON storage makes every edit
expensive and every derived view a full scan.

---


### Current ownership and shared projection

| Fact | Current owner | Problem to remove |
| --- | --- | --- |
| Pipeline phase | `contracts.STATES`, `NEXT_ROLE`, `PASS_STATE`, `RETRY_STATE`; `Ledger.accept/fail/findings` | `hold`, `failed`, `investigating`, and `approval_needed` replace the phase, so each hold stores a separate resume state. |
| Candidate and verdicts | Issue's `candidate` and `evidence`; job's candidate snapshot | One issue, one active job, one PR is embedded in `_current`, `reserve`, submission markers and all transitions. |
| Eligibility | `Supervisor.choose`, `reconcile_current`, `IssueActions.validate_action` | Several dictionaries and strings independently prevent dispatch. A row can retain dependency, environment, ownership and operator holds at once. |
| Attention | `control.attention_kind` | Different precedence from the issue panel: dependencies suppress attention, while the issue panel can prefer a hold. |
| Issue panel status | `issues.presentation` | Combines inventory's GitHub cache, ledger facts, PR ownership, free text and action availability. |
| Activity grouping | `ui/app/utils/factory.ts:groupCards` and `workbench.ts:ledgerLook` | Recomputes phase from ledger states, candidate presence and jobs. |
| Issue pipeline | `ui/app/utils/pipeline.ts:locate`, `reached`, `stoppedAt` | Independently infers phase from status, reason code, review rows, PRs and possible actions. A hold after audit can appear at a different stage on Activity. |
| Human decisions | `issue_actions.decide` plus actions JSON and GitHub synchronization | Immediate local mutations have a decision-version fence, but cross-store effects are not one transaction. Preserve the fence in the new store. |
| Terminality | `Ledger.cancel`, external closure and merge reconciliation | `cancelled` means both an open issue's stopped attempt and an externally closed issue. These are different facts. |
| Events | `Ledger.event` and ad hoc mutations | Generic `accepted`, `state_changed`, and specialized events do not consistently capture before/after phase and gate. |
| Observation | `Supervisor.cycle` | Repeated whole-ledger reads/writes and per-issue remote calls consume the observation budget. SQLite fixes the former, not GitHub latency by itself. |

Concrete cancellation regression: `control.requests` calls `ledger.cancel(job['issue_key'], request['reason'])` when an operator stops a non-recovery job. It does not close the GitHub issue. `presentation` intentionally offers Resume for this case. Treating every `cancelled` ledger row as GitHub closure would hide open work as completed. The new model stores a confirmed GitHub closure separately from a stopped execution; wording never determines which happened.

### Observation ordering

Concurrent readers must not overwrite newer GitHub facts with a delayed older response. Compare authoritative remote revision/update identity plus a monotonically allocated collection generation; an arrival timestamp alone is insufficient. An ambiguous or partial result records uncertainty and requests reconciliation. A missing page never proves an issue closed.

### Entities and ownership

- **Issue** owns GitHub identity, observed title/body/state/state reason, observation metadata, and an immutable requirements revision. It does not own candidate, retry budget or pipeline phase.
- **Unit** owns one delivery: repository, base branch, membership generation, current lifecycle state, current candidate, budget epoch and policy revision. A single issue is a unit of one.
- **Membership/part** joins an issue to a unit, pins its requirements revision, records its part branch and accepted output, and preserves historical membership when removed. Each issue has at most one live membership. A unit may have many part attempts running simultaneously.
- **Attempt** is one execution: build part, integration, review, audit, recovery or merge. It pins unit generation, requirements, inputs, candidate when relevant, and a fencing generation. A worker result is a proposal; only a validated transition accepts it.
- **Candidate** is an immutable revision of a unit PR, identified by repository, PR, head, base, membership/requirements revision and policy revision. A pointer chooses the current candidate. Verdicts, CI evidence and verification records reference that candidate; they are not copied mutable issue flags.
- **Gate** is a durable requirement or external-effect intent, with a code, scope (`unit`, `member` or `candidate`), subject, structured parameters, status and provenance. Approved human verification, CI observations and resolved environment repairs remain evidence after their current gate clears. Scope determines which assignments it can prevent; display priority does not broaden its authority.

Issues expose the states requested by #209 through a single server projection: `untracked` (known GitHub issue never admitted), `queued` (admitted, with no started work), `in_unit` (active membership without a current blocker), `blocked` (this member cannot advance because of an applicable member or unit gate, even while siblings continue), `done` (confirmed merged delivery or GitHub completed closure), and `dropped` (confirmed not-planned/duplicate closure). For unbatched queued work, the unit of one already exists. `in_unit` therefore does not mean “batch only”; membership details distinguish the two. Historical membership cannot make a reopened GitHub issue immediately done again. An issue's projected state is not another column to mutate independently.

### One state, one current blocker

Recommended unit lifecycle states are `queued`, `building`, `integrating`, `reviewing`, `auditing`, `merging`, `merged`, `closed`, and `dissolved`. The names describe the phase, including time waiting for a worker. A state does not assert that a worker is running. Execution activity comes from current attempts.

`merging` includes waiting for human approval, verification or GitHub merge eligibility. Verification requested during review or audit retains that phase. `closed` means all delivery work ended without a merge, with an explicit closure cause; `dissolved` records an operator decision to release batch members. A stopped worker is never a terminal unit state.

A unit has zero or one **current blocker**, selected centrally from applicable unresolved gate/observation facts only when the unit has no permitted forward work. A member may have its own single current blocker while other members continue. A blocked part does not make the entire unit blocked while independent parts remain runnable or active. Use either a store view or one transition-owned reference to the selected gate; do not duplicate mutable blocker code/details on the unit and gate. Several independent facts may remain unresolved, but only one governs the displayed next action and dispatch. Removing one cause recomputes the selection from the same transaction snapshot; it never silently deletes another cause.

Recommended priority, with structured subcodes:

1. Terminal state: no current blocker and no new dispatch.
2. `operator_pause`, `operator_backlog`, `label_hold`, `membership_decision`, `operator_sync`, `external_change`, `ownership_conflict`: explicit intent and ownership protections.
3. `budget_exhausted`, `approach_required`, `decision_required`: an operator decision is required before new code work.
4. `repository_setup`, `environment`, `observation_unavailable`: admission, capabilities or authoritative evidence unavailable.
5. `dependency`: unmet external prerequisites or a dependency cycle. An internal predecessor waiting during parallel build is a **part eligibility fact**, not a blocker for unrelated runnable parts.
6. `recovery_pending`, `cleanup_pending`, `provider_unavailable`, `capacity`, `retry_delay`: automatic recovery or scheduling waits.
7. `verification_human`, `verification_ci`, `approval_required`, `changes_requested`, `merge_checks`, `merge_conflict`: candidate gates applicable at the current phase.

This is ordering for presentation, not permission to bypass lower gates. Applicability is checked against the prospective assignment: a unit gate applies to every new assignment, a member gate applies to that member and dependent parts that need its output, and a candidate gate applies to integration or later work that consumes that candidate. Dispatch requires **all applicable hard gates** to be satisfied, except the gate that a particular recovery/recheck action is explicitly authorized to resolve. Terminal observations are processed before selecting blockers. An explicit pause is never replaced by an automatic dependency wait. “Recheck environment” clears only the environment requirement it proves repaired; it cannot release an operator pause or approve a candidate.

**Recovery and cleanup scope:** a failed part opens `recovery_pending` for that member, and its attempt retains its own cleanup lease. It does not gate independent build parts or recovery for a different member. `environment` is member-scoped when the capability is specific to that part, and unit-scoped only when independently observed common infrastructure prevents every part. A candidate-bound review/audit recovery prevents combined advancement but does not prevent explicitly scheduled independent remediation. Integration waits for all required current outputs. At that point the view may select one missing member gate as the unit's current blocker, retaining the member subject. Shared `budget_exhausted`, an operator pause, membership uncertainty and repository access restrictions apply to the entire unit.

Candidate invalidation is an immediate transition even while paused: archive the old evidence, fence attempts, select the new pending work phase, and retain the operator pause. This avoids resuming an obsolete audit after the base or requirements change.

### Transition matrix

Every transition checks an expected entity version and assignment generation, updates records and appends its event atomically. Remote work happens after commit, with durable intent and idempotency identity.

| Trigger | Preconditions | Result | Budget |
| --- | --- | --- | --- |
| Admit issue | Open issue; repository enabled; no live membership | Create unit of one in `queued`; pin requirements; select admission gates | New epoch, three failures |
| Approve combine proposal | Same repository/base; no human ownership; all external prerequisites closed; no live writes or cleanup; approved exact membership revision | Create unit; transfer memberships and reusable part refs atomically; retire source units with reason `combined` | New unit epoch; retain prior histories |
| Reserve first build part | Runnable part and capacity; all applicable gates pass | Unit `building`; reserve attempt and slot/lease before launch | No charge |
| Accept part result | Assignment current; expected requirements/base/input revision; independently confirmed commit | Part output accepted; newly eligible descendants may run | No charge |
| Finish all parts | Every live member has accepted current output; cleanup complete | Batch `integrating`; one integration assignment | No charge |
| Publish valid candidate | Current unit/membership, valid publication marker, independently confirmed PR head/base | Unit `reviewing`; select current candidate | No charge |
| Review passes | Verdict bound to current candidate | `auditing`; persist review evidence | No charge |
| Audit passes | Verdict bound to current candidate | `merging`; persist audit evidence and required gates | No charge |
| Needs human/CI verification | Valid current candidate and exact instructions | Retain phase; add verification gate; do not publish passing technical verdict | No charge |
| Verification satisfies | Fresh candidate-bound evidence; human approval newer than request or allowed exact CI result | Clear that gate; resume remaining technical work | No charge |
| Execution/code failure | Current, unfenced attempt | Record failure; retain retry phase; queue recovery scoped to that part or candidate. Independent parts continue below the shared failure limit | Charge once |
| Review/audit rejects | Current candidate | Invalidate downstream evidence; return to `building` for a singleton or `integrating` for a batch, scheduling affected part revisions as needed | Charge once |
| Recovery diagnoses repairable environment/prerequisite/valid prior publication | Exact failed attempt and independently confirmed facts | Correct charge by append-only adjustment, set appropriate gate or accept recovered output | Reverse that charge once |
| Base conflict | Candidate still owned; merge not confirmed | Invalidate verdicts; fence obsolete merge intent; rebuild singleton or integration at current base | No charge |
| Pause or stop | Expected current version | Record operator pause; fence relevant attempts; retain phase and refs | No charge |
| Resume/recheck | Explicitly authorized action and expected gate/version | Resolve only intended gate; recompute all others | No reset unless explicit retry |
| Confirm PR merged | Independently observed merge SHA for owned candidate | `merged`; fence obsolete attempts; persist delivery memberships; enqueue member issue closure checks | No charge |
| Observe GitHub closure | Authoritative observation with state reason | Singleton `closed`; batch membership change requires an explicit include/drop decision; preserve work | No charge |
| Drop member | Operator chooses exact membership generation; cleanup confirmed; dependent retained parts have an explicit dependency decision | Remove live membership, keep history/branch; create standalone queued unit; invalidate combined candidate and affected descendant outputs/input manifests | No silent budget reset on retained batch |
| Dissolve batch | Operator chooses exact generation; fence/stop then confirm cleanup | `dissolved`; release members to standalone queued units; preserve refs/history; supersede old candidate | Explicit new epochs for new units |

A singleton may keep the present direct-build-to-PR path and skip `integrating`; both paths produce the same candidate contract. The integration phase is compulsory when combining parallel parts. No batch review/audit may run against an individual part PR.

### Parallel parts and the shared three-failure budget

#207 already resolves three product choices: proposals enabled by default but never auto-applied, one shared three-failure budget, and no member-count cap. Do not reopen them as unanswered design questions.

- Dispatch parallel independent parts up to available Build capacity. Pin each part to an input manifest: original base plus exact accepted predecessor commits. A dependent part starts only after predecessors have built; “starts later” alone is insufficient if their code is absent from its branch. Integration merges in dependency order and validates the combined result.
- Cyclic internal dependencies cannot produce runnable work. Surface `dependency_cycle` with the actual cycle and request a revised split/combined implementation plan; do not burn worker attempts discovering the cycle repeatedly.
- Charge build, integration, review and audit failures to the unit's current budget epoch. Unique charge identity is `(attempt_id, charge_kind)`; retries of the same result cannot double-charge. Infrastructure failure, environment repair and base-conflict rebuild remain uncharged. Recovery failure does not invent a second charge for the original failure.
- On the transaction that records the third charge, set `budget_exhausted` and fence every still-active assignment atomically. Stop further launches. Other already captured or in-flight results retain their reports and branches, but cannot add another charge or advance the exhausted unit after that fence. Thus concurrent failures cannot race through to a fourth charged attempt. Preserve the raw failure observations rather than misreporting them as successes.
- A failure below the shared limit blocks its own part and descendants requiring its output. Independent parts continue; their launch is not conditioned on that member's recovery or cleanup. Only exhaustion or an explicit unit-scoped operator/safety gate stops the whole unit.
- Already accepted parts remain reusable after exhaustion. No integration/review proceeds until the operator chooses drop, hold or a documented retry. The three-failure budget is a failure limit, not a cap of three parallel parts.
- Dropping the failed part needs an explicit continuation decision for the retained unit; otherwise its shared budget is still exhausted. Recommended UI: “Drop #N and retry remaining work”, with both decisions in one transaction and a new budget epoch, retaining every prior charge. Dropped work receives its own history and a fresh epoch only through that explicit choice. Automatic batch proposals must never reset or evade an exhausted budget.
- Dropping a prerequisite member cannot leave a retained descendant pointing to its now-excluded commit. The drop preview lists affected descendants, accepted outputs and active assignments. The operator must choose to retain the prerequisite, remove/replan the dependency in the descendant's requirements, or wait for the prerequisite as external work. On confirmed drop, fence affected assignments, invalidate descendant outputs and integration evidence, and build new input manifests from the retained member graph. Unaffected parts remain reusable. No old branch containing dropped code is silently accepted into the retained delivery.
- Failure attribution names member IDs when known and can be unit-wide when the problem is integration. A worker cannot automatically drop a member because it is difficult.

### Candidate and membership safety

1. Candidate identity includes a digest of all live members, requirements revisions, part commits, target base and policy. Replacing any input invalidates current technical verdicts, verification evidence and merge intent.
2. Keep one current PR candidate per unit, but preserve candidate history. “One PR” means one delivery PR, not one irrevocable candidate revision.
3. Add a versioned unit submission marker containing unit ID, generation, publication attempt and head; validate its complete member manifest from the store. Continue reading legacy issue markers during migration. Workers cannot choose membership by editing closing keywords.
4. Before publication and merge, verify that PR closing keywords cover exactly the intended live member set in this repository and that the PR does not unexpectedly claim unrelated issues. Merge closes all live members; durable follow-up records repair any GitHub auto-close omissions without re-running the merge.
5. Membership changes fence and supersede existing integration/review/audit/merge assignments. A lost HTTP response to merge must be reconciled before permitting drop or dissolve: never expose members as new work while the old PR may already have merged.
6. Reopened issues are new requirements/queue decisions, with historical delivery retained. A merged unit remains merged; reopening one member does not reopen the whole batch.
7. A PR 404 on an open unit is `observation_unavailable` until absence versus lost access is established. Confirmed issue closure may retire singleton work while preserving an unreadable PR reference; a 404 alone is never proof of successful completion.
8. Provider and slot leases remain owned until teardown is confirmed. Clearing a logical active attempt does not free its physical capacity prematurely.

### Shared stage, event and action contract

Create one backend `UnitView` builder shared by status, issue inventory/detail, CLI and future event feed. It returns:

- `unit_id`, `unit_version`, `state`, `stage_code`, `current_blocker` (nullable structured code and subject);
- stage progress derived from current candidate evidence, active attempts and membership, including Build part counts and Integrate;
- one `display_status`, `attention_kind`, human-readable explanation and allowed actions;
- candidate identity, member summaries and paged links to attempts/artifacts/events;
- observation freshness separately from the lifecycle state.

The UI maps stage codes to visual assets only. Delete independent lifecycle inference from `pipeline.locate`, `workbench.ledgerLook`, Activity grouping, and action-based inference once all endpoints carry this contract. Wording lives in the server presentation catalog keyed by code and typed parameters. No comparison of prose, PR presence or “does this row offer Retry?” determines state.

Events have an increasing database sequence, kind/version, timestamp, subject unit/issue/attempt/candidate IDs, actor, correlation/request ID, before/after lifecycle and blocker references, and a bounded structured payload. Emit an event in the same transaction as every accepted transition; operational heartbeats need not create lifecycle events. Member views link to the unit event rather than copying one event per member. Attempt output is paged from artifacts, never embedded in change events or state summaries.

Required event kinds cover creation/admission, membership change, part accepted, candidate selected/superseded, phase changed, gate opened/resolved, attempt reserved/finished/fenced, budget charged/corrected/reset, decision applied/rejected, merged, closed and dissolved. Replay of one idempotency key yields the original result, without another transition/event. This gives #169 a safe sequence cursor without introducing a second authority for lifecycle state.

### Observation and validation commitments

“All open units every cycle” requires a repository-scoped observation pass as well as SQLite. Batch GitHub issue/PR/check reads where possible, persist observations once, and evaluate every open unit against that snapshot. Do not hold a database write transaction while waiting for GitHub or a runner. Fresh observations still must be fetched at publication/merge and authority boundaries. If GitHub rate limits prevent complete observation, report the incomplete cycle and stale units explicitly; do not claim the cycle guarantee was met. Measure both local evaluation time and external freshness under the same live workload.

Required lifecycle tests include every transition plus: open stopped issue versus closed issue; explicit pause plus open dependency; environment recheck under a human hold; candidate change during review/verification/merge; closed/deleted PR with lost-access ambiguity; repeated result and repeated decision identity; three simultaneous failures; third failure racing a success and new dispatch; drop/dissolve racing publication or ambiguous merge; membership/requirements changing mid-part; internal DAG inputs and cycles; a failed part leaves independent parts running; recovery and cleanup scope do not stop independent dispatch; dropping a prerequisite invalidates affected descendant manifests and excludes dropped code; no stale evidence after integration/base changes; all API/CLI/UI surfaces returning the same stage/action for the same snapshot.

### Approved lifecycle behavior

These defaults were approved with the architecture:

- **Combine existing work:** initially allow combine only after active executions and cleanup finish; preserve branches and require rebuilding/integrating stale candidates. Do not adopt an in-flight merge.
- **Budget reset after dropping:** use an explicit combined “drop and retry remaining” operator action. Preserve the old epoch and charges; no automatic reset.
- **Closure of a batch member outside Forge:** pause integration/merge with a membership decision. Other already accepted parts remain preserved; never silently remove the closed member's code or rewrite its requirements.
- **Unclear migration phase:** retain a coded `legacy_reconciliation` blocker with the original record reference when the old hold has no reliable resume phase. Never infer an audited phase merely because a PR exists.
- **Dissolve after merge:** reject because the delivery is complete; preserve all histories. “At any time” in #207 should mean before confirmed merge, with pending external merge first reconciled.

### Operator decisions (2026-10-05)

| Decision | Approved behavior | Boundary |
| --- | --- | --- |
| Remove a GitHub hold label | Resume eligible work automatically after a fresh observation clears the label gate | A separate operator pause, dependency or other applicable gate remains authoritative. |
| Closed externally as completed | Show `done`, with GitHub closure provenance | Do not call it merged without a confirmed merge. A stopped execution is not closure. |
| Repository setup becomes ready | Resume eligible work automatically | Verify readiness for the exact repository, target branch, controller identity and policy; clear only that setup gate. |
| Retention | Finished raw logs: 30 days; events: kept indefinitely | Preserve summaries, normalized results, decisions, candidate proofs and immutable input manifests. Required evidence is not a disposable log. All event kinds are kept; the former 90-day telemetry-event deletion proposal is withdrawn. |
| Durability | `synchronous=FULL` | No runtime downgrade to `NORMAL`. |
| Existing PR in a batch | Allowed, with operator approval of each batch | Quiesce execution/cleanup and reconcile any uncertain merge first; preserve useful branches and revalidate combined candidate evidence. |
| Rollback | None; one-way migration with no JSON switch-back | Keep one immutable pre-migration source copy for diagnosis only. Do not expose a restore-to-JSON command or restart old writers against it. |

Output APIs distinguish available, expired-by-policy, not-yet-captured and
corrupt/missing artifacts. Expired logs preserve outcome and summary. Missing
current result/evidence cannot be treated as passed verification. The retention
policy is approved; its scheduled deletion implementation belongs to a later
phase and is disabled in the foundation.

The prior 30-day JSON rollback window is withdrawn. The proposed seven-day parity
soak is a migration release decision; it does not block this inactive foundation.
Activation requires complete deterministic parity, crash/resume and integrity
checks on a coherent snapshot. SQLite backup frequency and response/debug-detail
pruning may be specified in their implementation PR; no additional deletion
policy is implied.

The earlier draft also proposes typed prerequisite findings for recovery as well as build workers. Preserve this requirement: a recovery report can identify member-scoped prerequisites with source requirements revision and quoted evidence; the supervisor independently verifies identities and applicability before changing gates or reversing charges. Retire prose-regex inference from normal reconciliation, keeping conservative, reported mappings only in the one-time importer.

## Storage and access design

This section defines the approved storage design. Live migration is a later
delivery phase. SQLite replaces the controller's mutable runtime records; a second
live copy in JSON is never a source of truth after that domain cuts over.

### Database ownership and durability

Use `<private-root>/state/forge.sqlite3`, owned by the Forge service account. The
directory is `0700`; the database, WAL, shared-memory files and backups are `0600`.
Validate ownership, permissions and symlinks before opening any existing database.
The database is never mounted into workers or the UI container. The controller
provides the existing authenticated API and selects explicit public fields.

Use Python's standard `sqlite3` library. Establish `journal_mode=WAL`,
`synchronous=FULL`, `foreign_keys=ON`, a bounded busy timeout and explicit
transaction boundaries on every connection. Fail startup when required settings
or schema compatibility cannot be established. Do not silently fall back to a new
database after corruption or deletion. Keep one immutable instance anchor outside
the database, containing only the expected instance ID and format signature. It
distinguishes first installation from a lost initialized store; it contains no
mutable generation or configuration. The database path is fixed by the host
launcher. Missing expected database/anchor pairs require explicit recovery.

Qualify the actual linked SQLite library during installation and updates:
`sqlite3.sqlite_version` must identify a WAL-reset fix (3.51.3 or later, or a
verified vendor backport such as 3.44.6/3.50.7). Distribution version branding alone
does not establish that the linked library contains the fix. Record the tested
library version and vendor evidence in the host qualification report.
[SQLite WAL-reset fix](https://www.sqlite.org/wal.html#walreset)

WAL allows readers and one writer to run concurrently on one host. Readers keep a
consistent snapshot for their short transaction. Keep the database on a local
filesystem, release read transactions before network calls or response streaming,
and retain `FULL` durability for launch intents and operator decisions. Monitor
WAL size and checkpoint time; use passive checkpoints during normal operation and
quiesced checkpoints for maintenance. [SQLite WAL documentation](https://www.sqlite.org/wal.html)

Writers use `BEGIN IMMEDIATE`; acquire no external locks and make no GitHub,
container or provider calls while holding the transaction. A bounded busy failure
returns a retryable coded error. Retrying an operator command uses its original
idempotency key; retrying an observation rereads its version fence.
[SQLite transactions](https://www.sqlite.org/lang_transaction.html)

### Schema boundaries

Columns below are the proposed logical schema. The first implementation PR will
include executable migrations and constraint tests. JSON columns are allowed for
bounded report details or raw observations, never as a replacement whole-ledger
document or a second copy of a normalized state field.

| Tables | Owned facts and constraints |
| --- | --- |
| `schema_migrations`, `store_metadata` | Applied schema version/checksum, instance ID, storage generation, per-domain backend authority, feature version. A binary refuses newer unsupported schemas. |
| `repositories`, `repository_observations` | Repository identity and the latest access, protection, inventory, deployment and refresh observations; observation type, revision, fetched time, error code and bounded details. Partial GitHub scans have a scan generation and cursor; missing pages never imply closure. |
| `issues`, `issue_revisions` | Unique `(repository_id, github_number)`; GitHub state/state reason, labels, assignees, observation provenance and current requirements revision pointer. Immutable revisions own title, body/content hash and canonical requirements snapshot/hash. The inventory and supervisor update this one pointer, while memberships retain their pinned revision. |
| `units`, `unit_issues` | Unit state, current candidate reference, primary blocker reference, version fence, budget generation and lifecycle timestamps. Membership has a stable ID, requirements revision, part state and historical exit reason. A partial unique index permits only one active unit membership per issue. Members must share repository and base branch, enforced by transition validation in the same transaction. |
| `issue_dependencies` | Directed prerequisite edges with source and requirements revision; explicit member mappings distinguish dependencies inside a batch from external prerequisites. |
| `candidates`, `candidate_evidence` | Immutable unit candidate identity: PR, branch, head, base, policy digest and requirements digest; review/audit/verification evidence references the exact candidate. New candidates invalidate previous gates by identity, not by clearing arbitrary nested fields. |
| `pull_requests`, `candidate_observations`, `check_runs`, `merge_requests` | PR discovery/ownership, current candidate observations, individual check identities/statuses and durable merge intents. An uncertain merge request stays reconcilable before membership changes or retry. Check settlement is a durable external effect, not inferred from a completed process. |
| `unit_gates` | Coded unresolved and resolved gates scoped to unit, member and optional exact candidate. A member FK and constrained subject key permit independent same-code member gates, with one unresolved row per `(unit, code, subject)`. Unit projection selects at most one primary blocker by shared precedence/applicability while retaining other facts. A part's recovery/cleanup gate stops that part and descendants; unrelated eligible parts continue below budget exhaustion. One cleared gate cannot erase another. |
| `attempts`, `jobs`, `job_leases` | Attempts record charge/no-charge classification and budget generation. Jobs record role, candidate, optional member, phase, result and execution fence. A job lease owns a unique resource slot until verified cleanup; candidate invalidation does not release a lease. Parallel part jobs are permitted, while review/audit/integration exclusivity is checked by transitions. |
| `job_artifacts` | Output, capture, observation and assignment-history artifacts, keyed by `(job_id, kind, sequence)` with content type, size and digest. Large output uses bounded chunks; list/status queries never fetch blobs. Keep already-redacted content only. |
| `events` | Append-only integer sequence, transaction ID, entity IDs, kind, time, actor reference and bounded payload. Index `(unit_id, sequence)` and `(issue_id, sequence)`; a global sequence is the cursor for #169. Store state/projection transitions once with old/new codes and versions. |
| `decisions`, `external_effects`, `check_requests` | Durable commands with unique request IDs, actor, expected entity version, status and result; outbound GitHub/process effects have stable identifiers and reconciliation status. Repeated requests return the existing result. |
| `discussions`, `messages`, `discussion_executions` | Conversation identity, optional member/unit context, auxiliary execution ownership and ordered message records. Auxiliary discussion executions share the provider-capacity reservation boundary but do not consume a unit failure budget; their DDL is outside the pipeline-job fixture. Transcript output remains an artifact instead of growing the discussion row. |
| `provider_cooldowns`, `provider_observations` | Account readiness, cooldown reason/retry time and the last provider observation. Credential contents remain in the credential store. |
| `memory_profiles`, `unit_environment` | Learned memory by repository/role with source-job provenance; explicit unit environment choice/override, retry tier and selected profile revision. Applying a memory retry preserves the candidate, budget and branch. Provider-selection and repository-discovery cursors belong to typed runtime controls. |
| `deployment_observations`, `environment_proposals`, `environment_scans` | Current deployment knowledge and typed environment/configuration proposals and their revisions. Accepting a proposal records a decision and an idempotent external configuration effect. |
| `setup_operations`, `client_operations`, `runtime_controls` | Onboarding/reset progress, approved client repository operations, activation and pause generations, supervisor heartbeat and operational cursors. |
| `configuration` | Versioned active runtime, repository and identity configuration with opaque credential references; JSON becomes an explicit import/export format. |
| `operator_sessions`, `operator_flows`, `client_connections`, `operator_audit` | Existing authorization epochs, expiry, token hashes, scopes and request audit records. Preserve revocation semantics: sign-out revokes that operator's approved agent connections. Pending login flows remain private and bounded by expiry. |
| `notification_outbox`, `notification_deliveries` | Durable enqueue IDs, delivery status, retry schedule and bounded diagnostic details. An observer owned by another installation remains separate. |
| `import_runs`, `import_sources`, `import_progress` | Frozen source hashes, importer version, per-table cursor/counts, validation report and completion status. |

Use foreign keys for ownership, unique constraints for identities and idempotency,
and checks for state codes, positive versions and valid budget values. Enable
foreign-key enforcement on every connection. Cross-row invariants such as a
candidate belonging to the current unit and active jobs belonging to an allowed
member are validated by transition methods under the write transaction, with
composite foreign keys where practical. [SQLite foreign-key settings](https://www.sqlite.org/pragma.html#pragma_foreign_keys)

The single database is not a reason to widen credential access. Identity client
secrets currently live inside `operator-auth/auth.json`; migration moves them to
the existing private credential store with an opaque reference in the database.
GitHub App keys and provider credentials stay there too. Personal GitHub admin
tokens remain on the operator's computer. This reference change must preserve
atomic activation and fail closed if a credential reference is missing. Config
files such as `runtime.json` and `factory.json` become import/export interfaces;
their active mutable settings live in versioned `configuration` rows by the final
phase. Host service definitions, image policy and immutable source files remain
installation inputs. During staged cutover a configuration domain has one selected
writer; phase 7 removes its live JSON writes. Only the immutable instance anchor,
launcher location, sealed backups, credential material and temporary IPC remain
outside the operational database. The active storage generation, configuration
revisions and migration journal live in database tables.
Worker assignment/result files remain temporary IPC artifacts, not authoritative
controller state. No worker can mutate a database row.

The earlier draft proposes compressed artifact storage and live-tail deduplication;
retain both as implementation options. Final output may supersede an identical
live tail after its digest is verified. Compression must record codec, raw byte
length and digest and enforce a bounded decompression size. Missing optional log
output can be shown as unavailable. Missing assignment inputs, result contracts,
candidate evidence or lease records must fail the affected transition closed; they
are not interchangeable with optional logs.

Use verified SQLite backups for database recovery and before schema changes.
The foundation provides the backup primitive without a schedule or deletion job.
Logs expire 30 days after completion in the later retention implementation;
normalized results, summaries, decisions, input manifests and all events remain.
A raw transcript needed by active recovery must first have its durable evidence
extracted before it can expire. The one-time legacy source copy is diagnostic
only and is never a runtime backend or rollback target.

The accompanying `state-and-units-of-work.sql` is an executable core-schema design
fixture, not a production migration. It proves key ownership, exclusive active
membership, slot uniqueness, append-only events and budget correction constraints.
The remaining tables follow the ownership boundaries above; their full DDL belongs
in the schema implementation PR. The fixture uses the same gate codes as the
lifecycle section and includes unit membership/fencing generations and candidate
input-manifest identity. Memberships reference immutable issue revision rows;
candidate manifests incorporate every pinned revision and dependency input. It
uses `STRICT` tables, supported by qualified SQLite
versions. Assignment acceptance checks the saved generation and manifest against
the current unit within the write transaction.

### Legacy field census

| Today (ledger field) | New home | Notes |
| --- | --- | --- |
| `state` | `units.state` + scoped `unit_gates` | — |
| `reason` | `unit_gates.code` + structured detail | Prose only in `detail.note` |
| `active` | `jobs`, `job_leases`; `merge_requests` | No pointer to keep in sync |
| `candidate` | `candidates`, `units.current_candidate_id` | — |
| `failures`, `budget_start`, `job.failure`, `job.history` | append-only `budget_entries`, `attempts`, recovery findings and `units.budget_generation` | History is a query; the launch input is kept as an artifact |
| `evidence`, `verified_checks`, `verification_request`, `merge_gate.approvals` | `candidate_evidence`, candidate-bound `unit_gates`, `candidate_observations` | Scoped to the candidate, so no clearing is needed |
| `requirements`, `title`, `github_observation`, inventory rows | `issues` + immutable `issue_revisions` (compare remote revision and collection generation, not response arrival time) | Inventory and supervisor share it |
| `blocked_dependencies`, `dependency_reason`, `declared_dependencies`, `dependency_body`, `dependency_evidence`, `dependencies_checked_at` | `issue_dependencies` + `unit_gates(dependency)` | Wording derived |
| `ownership_wait` | `unit_gates(ownership_conflict)` with a structured detail (`login`, `pr`) | — |
| `label_hold` | `issues.labels` → `unit_gates(label_hold)` | — |
| `operator_hold`, `operator_sync`, `operator_request`, `actions.json`, `operator-requests.json`, `check-requests.json` | `decisions`, `unit_gates(operator_pause \| operator_backlog \| operator_sync)`, `external_effects` | — |
| `environment_hold`, `prerequisite_hold` | `unit_gates(environment \| repository_setup)` | The saved stage disappears |
| `environment_choice`, `_override`, `_retry`, `_label`, `memory_retry` | versioned `unit_environment` | `environment_label_pending` becomes the outbox |
| `decision_request` | `unit_gates(decision_required).details_json` | — |
| `checks_settled` | `check_runs` + `external_effects` | — |
| `last_summary` | The latest job summary for the unit | Derived |
| `retry_after` | `unit_gates(retry_delay)` with a bounded retry timestamp | — |
| `closed_as` | `issues.state_reason` | — |
| `deployed`, `deploy_failed` | `deployment_observations` bound to candidate | — |
| `last_observed`, `settled_observed`, `budget_interruptions` | `units.observed_at`, `units.observe_interruptions` | — |
| `decision_version` | `units.version` | — |
| Ledger `slots` | `job_leases` with unique slot and verified cleanup | — |
| `provider_cooldowns`, `provider_retry_after`, `provider_cursor`, `memory_profiles`, `discovery_observed` | `provider_cooldowns`, `runtime_controls`, `memory_profiles`, repository observations | — |
| `discussions.json` | `discussions`, `messages`, auxiliary `discussion_executions` | `reserve_logins` becomes one query |

---


### Access layer and transition boundary

`forge/store/` contains connection setup, ordered SQL migrations, typed queries,
domain transition methods, artifacts and import tools. Callers never receive a
mutable whole-store dictionary or issue unrestricted SQL from UI/CLI adapters.

The interface separates:

- `read_snapshot()` with paged unit/issue lists, counts, details, bounded recent
  events and lazy artifact reads;
- `apply(command, expected_version, request_id, actor)` for operator mutations;
- `observe(entity_id, expected_version, observation)` for controller results;
- `reserve_job`, `record_result`, `fence_job` and `release_lease` for execution;
- `events_after(sequence, limit)` for the future change feed.

A mutation validates its entity version and exact candidate identity, changes only
affected rows, recomputes the shared lifecycle projection, appends its transition
events and records any required external effect in one transaction. A failed or
rolled-back transaction emits no event. Repeating an idempotent command emits no
second event. The supervisor can record a new observation without manufacturing a
state transition when the codes did not change.

#168's shared stage function belongs in this boundary: it consumes typed unit,
issue, gate and job facts and returns the same stage, state and reason code for
both API views and dispatch eligibility. The frontend maps these codes to visuals.
It does not parse prose. Transition records compare the old and new canonical
projection; they do not diff or deserialize every issue in every transaction.

Avoid changing every production reader in the schema PR. Ship the inactive store
and transition tests first; then a frozen import/parity tool; then cut over each
domain with exactly one writer. A temporary compatibility adapter may emit legacy
API shapes from SQL queries. It may not reconstruct the whole old ledger on every
read or implement whole-document writes.

## One-way migration and parity

### Source inventory

The importer must cover more than `AtomicStore`. Current mutable sources include:

- `state/factory/factory-ledger.json`: issues, jobs, slots, events, provider state;
- `state/issues/inventory.json`, `actions.json`, `discussions.json`;
- factory operator requests, check requests and supervisor observation;
- deployments, environment scans/proposals, repository access/setup observations;
- onboarding/setup and client-operation state;
- active `runtime.json`, `factory.json` and operator-managed configuration revisions;
- operator auth/session/connection state and request audit JSONL;
- activation/pause generations, service heartbeat and notification outbox/status.

The implementation inventory must list every direct `read_json`, `write_json`,
`atomic_json` and marker access and classify it as migrated runtime state,
configuration, credential material, temporary IPC or independent observer state.
CI checks must reject remaining runtime JSON writes after phase 7. A ledger-only
migration cannot satisfy #209's final acceptance.

### Domain authority during staged delivery

`store_metadata` records the authoritative backend for each runtime domain. Every
supported launcher and adapter reads this routing under maintenance. A domain
moves as a dependency-closed set of writers and readers; phases 3 and 4 must ship
together wherever a legacy reader would otherwise consume frozen state. There
is never a live dual-write period for the same fact.

Capture briefly quiesces all writers for a coherent all-store snapshot. During
cutover, only migrated domains have their live JSON originals sealed and retired;
unconverted configuration/authentication domains resume their existing sole JSON
writer. The complete snapshot remains sealed separately. Each later domain
migration takes a fresh coherent snapshot, imports that domain, and changes its
recorded authority under maintenance. Phase 7 requires every runtime domain to
use SQLite. Once a domain switches, its JSON writer never resumes. Failures stop admission
and use resumable import or a forward repair of the authoritative store; the
frozen source is available only for diagnosis.

### Import protocol

1. Inspect the installation, schema support, free space and all source stores.
   Produce a plan bound to source revision, storage generation and inventory.
2. Enter maintenance: reject new writes and job admissions, drain existing jobs
   through result collection and verified cleanup, then stop every runtime JSON
   writer. A manual pause alone is insufficient: API actions, auth, setup,
   discussions, inventory refresh and deployment observers also write. Acquire a
   migration lock recognized by new supported binaries. Old binaries do not know
   this lock: stop their services/timers and API entrypoints, and replace managed
   launchers with a backend/schema guard before switching storage. The guard
   rejects a source version that cannot read the selected database. Arbitrary
   manual execution of an old checkout is outside that launcher guarantee; the
   procedure seals migrated-domain JSON read-only and records any old process it cannot stop
   as a blocker. Report any uncleaned job and resume later; never infer process
   termination from elapsed time.
3. Copy all source files and initialization markers into a private immutable
   backup. Hash a manifest and fsync the snapshot before importing. Import only
   this frozen snapshot; resumability is against these hashes, not moving live
   files. A dry run imports an existing coherent private snapshot and never selects
   the new database or restarts services. Capturing a coherent snapshot from the live
   legacy stores is a separate planned operation that can require a short maintenance
   window; the CLI names that effect explicitly. It must not describe a live capture
   that pauses writers as read-only. Admission draining, controller cutover and
   service restart belong to explicit apply operations.
4. Import bounded batches into a staging database. Commit source cursors and rows
   together. Stable legacy IDs and unique import keys prevent duplicates after a
   crash. Preserve old event sequences and timestamps, job IDs, cleanup fences,
   budget generations, candidates and approvals; do not reclassify historical
   failures or authorize work during import.
5. Validate schema constraints, `integrity_check`, `foreign_key_check`, row counts,
   artifact sizes/digests and domain ownership invariants. Compare legacy and SQL
   public views from the same frozen source, including paging and lazy output.
   Report each intentional lifecycle correction separately; an unexplained parity
   difference blocks activation. New unit membership maps each legacy issue to a
   deterministic unit of one, with no batch proposals activated by import.
6. Mark the import complete in staging `store_metadata`, create a verified SQLite
   backup, checkpoint and close staging connections, then rename the database to
   the fixed live path and fsync the directory under maintenance. Install/verify
   the immutable instance anchor. The completed metadata at the guarded live path
   is the selector; there is no mutable JSON backend pointer. If a crash separates
   rename and anchor creation, the launcher refuses the incomplete pair and the
   migration resume completes it using the staging journal and sealed source
   manifest. Keep migrated-domain JSON and the full backup sealed and read-only. Restart
   compatible services in read-only reconciliation mode first, verify GitHub and
   process ownership, then resume admission. Each domain has one active writer
   throughout; never dual-write JSON and SQLite.

Crash tests cover every boundary, including a committed import batch without a
finished CLI response, completed staging DB before selection, selection before
service restart and restart during reconciliation. A resume checks the recorded
operation/generation and continues the existing operation; it never creates a
fresh empty store as a recovery shortcut.

### Backups and forward recovery

Use the SQLite backup API for consistent database backups. Copying only the main
DB file while WAL is active can omit committed transactions.
[SQLite backup API](https://www.sqlite.org/backup.html)
Verify each backup separately and record schema, instance and generation metadata.

Legacy migration is one-way. The diagnostic pre-migration copy is never restored
as an operational JSON store, and there is no export-and-switch-back path. A
failed import resumes against the same source manifest. A post-cutover failure
requires repairing or recovering the SQLite store, reconciling current worker
ownership and uncertain external effects before admission resumes.

Database disaster recovery must preserve current revocation epochs or invalidate
sessions and pairing grants explicitly. Recovering an older database must never
silently revive revoked connections, repeat a merge, or reassign an uncleaned slot.
This requirement applies to a future recovery tool; this foundation only creates
verified backups and does not activate restored data.

### Measured release gates

The live dry run happens on a private snapshot and produces a redacted report:
source bytes, rows per table, artifact bytes, database/WAL bytes, import and resume
times, parity mismatches and ownership checks. No issue bodies or logs belong in
the PR or benchmark output.

Measure old/new code on the same source snapshot and host: cold and warm status
requests, page/detail/output queries, transaction latency and a complete cycle.
Report p50/p95, query counts, elapsed time spent in GitHub and storage, and every
open unit's observation freshness. The target is single-digit milliseconds for
status data and observation of every open unit in seconds. Storage changes alone
do not remove GitHub rate limits: conditional/batched fetches and fair scheduling
must be measured independently, and API failures must remain visible as stale
observations rather than being reported as a successful complete cycle.

Run transition, stale-version, idempotency, candidate-change, concurrent-reader,
competing-writer, process-crash, disk-full, corruption, missing-file, permission and
backup/restore tests. Publish actual measurements when the implementation exists;
this design does not claim a live dry run or performance result.

## Design validation and reported measurements

### Reproducible core-schema checks

The adjacent [SQL fixture](state-and-units-of-work.sql) exercises the core ownership
and persistence constraints. It is deliberately incomplete and is not installed
or imported by Forge. Run only against the script's disposable in-memory database:

```sh
python3 docs/design/check-state-and-units-of-work.py
```

The check covers 21 rejected mutations, independent same-code member gates,
verified lease cleanup, foreign keys and database integrity. It does not prove
all transitions, migrations, process races or performance; those are implementation
release gates below. No production database is read by this check.

### Earlier author's prototype report (2026-10-05)

The initial PR #210 author reported the following measurements using a private
copy of the live state and a simplified prototype schema. We preserved these
figures from commit `2dd3f89`; this review did not rerun that experiment. They show
potential benefit, not a verified full-store migration or production performance
result. A coherent snapshot across all mutable writers was not established by
that report. The phase 2 importer must reproduce counts and parity with the full
schema and explicit source hashes before activation.

| Source | Size | Rows |
| --- | --- | --- |
| `factory-ledger.json` | **64.6 MB** | 112 issues, 878 jobs, 3,135 events, 0 live slots |
| ├ `jobs` | 58.7 MB (91%) | `capture` 28.4 MB (logs 26.4), `observation` 20.4 MB (logs 20.2), `failure` 3.6, `history` 3.1, `runner_config` 1.1, `ci_evidence` 0.9 |
| ├ `issues` | 4.1 MB | `failures` 3.4 MB, `evidence` 0.4 MB |
| └ `events` | 1.5 MB | Largest kinds: `assigned` 791, `runner_released` 791, `accepted` 415; at most 129 per issue |
| `issues/inventory.json` | 2.5 MB | 8 repositories, 1,010 issues, 12 PRs |
| `deployments.json` | 0.45 MB | — |
| `issues/discussions.json` | 0.17 MB | 7 threads, 17 messages |
| `issues/actions.json` | 0.07 MB | 17 requests (11 applied, 6 rejected) |
| Orphan `.pending-1fh9yc66` | 12.1 MB | A leftover from an interrupted rewrite on Sep 29 |

| Measurement | JSON today | SQLite prototype |
| --- | --- | --- |
| Parse the ledger | 0.43 s | — |
| Validate (full `json.dumps`) | 0.25 s, ×2 per transaction | Constraints, per row |
| Rewrite + fsync | 0.52 s | — |
| **One write transaction** | **≈1.4 s** | **1.6 ms** (`synchronous=FULL`), 0.1 ms (`NORMAL`) |
| One `ledger.read()` | ≈0.7 s | Snapshot `SELECT`s |
| Full open-board query (112 units + 6 jobs + 3 events each) | Whole-file parse | **0.3 ms** |
| Import time | — | 1.7 s |
| Database size | 64.6 MB | **34.3 MB**: artifacts zlib 48.8 → 11.3 MB (13.8 MB on disk); jobs 10.9; issues 3.4; units 4.2; events 1.8 |
| **Round-trip parity** | — | **0 mismatches** over 112 issues and 878 jobs (artifacts reattached); 3,135 events equal |


## Operator and agent workflow

The following is a proposed command surface, not commands available today. Extend
the standalone client's existing `forge server` group. The client handles SSH,
host jobs, status polling, and any transfers; the operator never copies state
files or enters a remote shell. The JSON interface and human wizard call the same
operations. Personal GitHub credentials remain local.

| Proposed command | Result and authority |
| --- | --- |
| `forge --json server state plan --ssh forge` | Read-only compatibility, size, disk-space, active-execution and source-store inventory; returns a plan ID and revision. |
| `forge --json server state capture --ssh forge --plan ID --revision REV --apply` | Creates a coherent private host snapshot under a planned maintenance window, quiescing all writers; returns a snapshot ID and resumes the existing backend. |
| `forge --json server state dry-run --ssh forge --plan ID --snapshot ID` | Imports that frozen snapshot into a separate database, runs parity and integrity checks, and returns counts/timings. It does not switch the backend, pause services or write to GitHub. |
| `forge server state migrate --ssh forge --plan ID --revision REV --apply` | Revalidates the plan, enters maintenance, drains/fences jobs and writers, backs up, imports, verifies, atomically selects the database, then resumes only after health checks. |
| `forge --json server state status --ssh forge --migration ID` | Durable progress, current authority by domain, completed checks, actionable error code, and exact resume command. |
| `forge server state resume --ssh forge --migration ID --apply` | Continues the same migration journal; never starts a second import or silently changes its source snapshot. |

An interactive wizard explains the current phase and waits only for decisions
that require a person. `--json` never opens a prompt or browser. Every mutation
accepts an idempotency key and expected revision, returns structured errors and
documents retry semantics through `forge commands` and the bundled agent skill.
Database maintenance requires SSH operator authority; an ordinary repository
agent connection cannot migrate, export private state, or restore backups.

`bin/forge update` detects the old backend and the required schema version. It
does not start new workers against a half-migrated database or silently let an
older binary resume writing JSON. Initial SQLite release leaves the source JSON
backup private and immutable. Final retirement removes live reads and writes,
not the recovery backup. New installs use the database directly.

## Observation and performance

Removing serialization alone does not make GitHub fast. The current supervisor
has a wall-clock budget and rotates issues; moving the same network loop behind
SQL would retain this limit. The cutover must change both local persistence and
observation scheduling:

1. Read all open units, latest repository observations, due work and requested
   checks in indexed snapshot queries. Do not read job artifacts or full event
   history to build a status response or choose work.
2. Batch lightweight GitHub issue/PR identity and revision observations per
   repository/installation. Record which units were actually covered by each
   successful response. Use bounded concurrency outside SQL transactions and
   respect GitHub rate limits. A failed batch records unavailable evidence; it
   never pretends its units were freshly observed.
3. Reconcile every open unit against that cycle's observations. Revalidate the
   exact candidate, current approvals, technical check identities, branch
   protections and bypass policy immediately before any merge attempt. A cached
   broad observation never replaces these final gates.
4. Publish durable external intents, claim available jobs, and dispatch after
   commit. Provider login, Docker launch, GitHub calls and retries never hold a
   database transaction open. An old process or attempt must fail its fencing
   check before publishing a new effect.
5. Carry unfinished remote observation work forward with explicit stale status
   and fair scheduling. Manual Check now and imminent merges get priority, but
   cannot indefinitely starve other units.

The acceptance test measures a complete observation sweep on a copy of the live
workload and then on the host with healthy GitHub access. It records open-unit
count, API requests and quota, p50/p95 sweep duration, oldest successful remote
observation age, units covered and deferred, query time, writer wait, database
size and WAL size. The target is a complete healthy sweep in seconds (initial
budget: ten seconds at the measured live workload), with every open unit
covered. Outages and exhausted upstream quotas must be reported as degraded;
there is no honest constant-time promise for arbitrarily many remote units.

The status API target is p95 under ten milliseconds for representative board,
issue, unit and paginated history requests on the host, excluding network
transport. Measure it cold and warm while the supervisor writes, using the same
input data as the JSON baseline. Output downloads are measured separately.
Bounded indexed queries are the baseline; remove the file-fingerprint cache only
after uncached database reads meet the target. A later event cursor (#169) can
invalidate optional presentation caches without reintroducing file scans.

**Time to first merge remains the product metric.** Record fresh CLI setup start,
ready-to-run, first accepted work, and first GitHub-confirmed normal merge. A
database migration preserves those timestamps; it must not restart the clock.
The full release proof includes a unit of one and a two-issue batch reaching a
normal merged PR, with no manual server shell steps.

## Delivery

The operator approved phase 0 of #209. It carries #168's shared
stage projection and transition events into the database model; the closed
issue's per-job JSON archive is not implemented.

| Phase | Reviewable deliverable | Release gate |
| --- | --- | --- |
| 0 | This lifecycle audit, schema, access boundary and migration design. | Approved architecture and operator decisions are recorded above. |
| 1 | Private SQLite store, schema, typed transitions and invariant tests, disabled for runtime use. | Constraint, concurrency and transaction crash tests pass. |
| 2 | Resumable import, backup and deterministic parity tooling. | Private live-state-copy dry run reports no unexplained differences; crash/resume leaves one import. |
| 3 | Supervisor and mutation paths use row operations; artifacts remain out of hot queries. | Single-writer ownership, cleanup and external-effect recovery survive restarts; measured observation sweep improves. |
| 4 | API, UI and CLI share projections and database queries. | Parity and host latency pass; every mutation/read family is accounted for. |
| 5 | Existing work represented as units of one. | Requirements, budgets, candidates, approvals and completion survive migration without being reset. |
| 6 | Approved batch proposals, parallel dependency-aware parts, integration, shared budget, drop and dissolve. | Two or more issues reach one normal merged PR; failure/drop/dissolve races are tested. |
| 7 | Retire operational JSON paths and finish update/backup documentation. | Static call-site inventory plus runtime instrumentation finds no operational JSON writes; forward-recovery rehearsal passes. |

The implementation can share groundwork between phases, but no phase silently
enables the next. Neither migration nor live deployment is implied by merging
this design. #209 remains open until all its acceptance criteria, including
batches, are met. Draft #166 remains separate until its outstanding first-merge
setup acceptance is complete.
