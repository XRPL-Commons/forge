# Repository planning

Open **Repository settings → Plan repository work**. **Triage now** requests a read-only pass over the repository's open issues. Nightly proposals use the same path, becoming due at midnight UTC. Relevant repository changes also request a pass: issue requirements, state, labels or assignees; delivery membership, candidate or completion changes; and operator edits to rules or notes. Events coalesce for thirty seconds, with at least five minutes between event-driven launches. Unchanged polls and the planner's own proposals or notes do not trigger another run. Forge keeps one planning worker at a time and launches it after ordinary pipeline work. Disabling proposals disables nightly and event scheduling; an explicit request remains available.

The steward plans a disposition for every captured issue: keep, close as already completed, relabel, park, group, order prerequisites, or ask a question. Nothing changes on GitHub while the proposal is being written. A failed or incomplete run leaves its diagnostic record and cannot publish a partial proposal.

## Review and accept

Each row shows its issues, one to four ranked alternatives, a recommendation and confidence, reasons, deciding rule quotes, supporting references, and any existing PRs. **Choose this option** selects a stored alternative using its row ID, choice ID, decision digest and current proposal version. It updates the row without approving it. Alternatives preserve that row's exact issue membership. Edit the reason or action parameters, reject individual rows, or accept rows together. **Accept all** becomes available once every page is loaded. Changing a group's membership requires a new complete proposal, so an issue cannot disappear between rows.

Planner rationale, reasons, notes and handoffs describe recommendations and their evidence. They cannot establish that an action was approved or applied. The page attributes planner notes and operator corrections, shows each row as awaiting approval, approved or rejected, and reports GitHub effects separately. Each issue shows its last observed open or closed state and observation time. The CLI returns these same recorded facts as `status`, `effects`, `members[].github_state` and `members[].observed_at`. A claim that underlying work is fixed does not mean its GitHub issue is closed. Original worker output remains immutable when an operator corrects proposal text or notes.

The captured row dossier has subject `{kind: "proposal_row", proposal_id, row_id, version}` and pins its planning context digest. Its immutable decision remains attached to the exact worker execution. Choosing an alternative records normal row history; manually editing a row clears the captured-choice association. Replaying the same choice request is idempotent, while a different choice needs a new request and the current version.

Acceptance pins the proposal version, repository rules, issue requirements, membership and candidate heads. Forge independently reads GitHub before acceptance and again before issue edits. If any authority changed, request a fresh proposal. A lost response can be retried with the same request identity.

Unchanged background observations preserve these pins. Collection generations and observation times track freshness separately from versions of issue facts and delivery decisions. Real changes to requirements, ownership, prerequisites or execution gates still invalidate approval. An inventory listing that repeats the same issue preserves previously collected complete requirements, but cannot renew the time of that complete observation.

All local decisions in an acceptance commit together. Issue labels and closures then run as durable effects with readback. A lost GitHub response is reconciled before another write. Parking adds the configured hold label and removes the ready label. Closing already completed work uses GitHub's completed reason; it does not claim a Forge merge or close an associated PR. Existing holds, cleanup requirements and failure charges remain effective.

A group becomes one delivery with one owning builder, tracked child assignments, one integrated candidate, and one Review/Audit sequence. Its source failure budget carries forward. Every existing PR must be listed explicitly. Choose either a new PR or one exact existing PR to continue. Continuing an existing PR explicitly transfers its implementation to the builder while preserving its original author; the approved author identity and head are rechecked. Other PRs remain open. Cross-repository prerequisites use `owner/repository#number` and wait for independent observation before work can proceed.

An accepted keep, group or prerequisite row can carry a brief for that exact chosen alternative. Its optional environment is typed as `{environment_id, services}` and must name a configured environment. Forge applies it only to the row's live delivery members, using normal environment observations and label effects. A conflicting operator override or explicit environment label rejects acceptance; a brief cannot clear independent holds. Selecting another alternative or manually editing the row does not carry the old brief with it.

## Rules and notes

Standing triage rules are plain text, versioned per repository. The planner must quote the exact deciding rule in each affected row, or use `null` when no pinned rule applies. With empty rules, every quote is `null`. A group row contains at least two distinct issues; every other action covers exactly one. Unchanged single issues use `keep` with empty parameters. The worker receives one captured decision-v1 dossier per possible row and a provider-compatible envelope containing `decisions` and bounded reply, notes and handoff metadata. Each returned decision names its dossier ID and digest; all recommended rows together must cover the captured backlog exactly once. Typed `wait_on` choices name another captured issue by repository ID and issue number, then use the existing prerequisite authority when approved. The schema is retained and hashed with its bounded planning context; ordinary row edits remain described by `forge --json commands`. The host still validates coverage, source identities and rule quotes before publishing a proposal. Editing rules invalidates pending acceptance against the old revision.

Repository notes record conventions, decisions, lessons, questions and intended next actions. Each note has source links, an immutable source identity, an author and revision history. Correct a note or archive it in the planning page. Issue-revision and candidate references become stale when that source stops being current; stale notes are excluded from the next planning context. Commit, document and decision references describe recorded evidence, not current execution authority. Update their note revision when the interpretation changes.

A completed planner may retain up to five sourced notes and one next-action handoff, together at most 32 KiB. These are written with the proposal after worker cleanup, using the exact captured issue/candidate identities or assigned repository commit. The handoff carries unfinished planning into the next pass. The planner may consolidate its own notes, but cannot overwrite an operator correction or a note edited after its context was captured. Conflicts and a full note store leave a receipt with the captured result; the proposal remains available. Invalid source identities reject the complete publication. Planner-generated sources use only `issue_revision`, `candidate`, `commit`, or `document`. Issue and candidate sources pair the exact captured revision/candidate identity with that same issue or PR URL. Commit and document sources use the assigned head with its commit URL or an immutable blob URL. This narrower source contract is captured with the worker input before its hash and size checks; operator notes and closing evidence retain their broader reference contract.

Context has explicit storage limits: fifty active notes, 8 KiB per note, 16 KiB of standing rules, and 512 KiB for a planning assignment and 4 MiB for its structured result. Issue excerpts are marked; repository access uses the read-only mirror and issue facts come from the captured input. Exceeding the assignment limit fails the run visibly rather than omitting issues. There is no issue-count cap on approved delivery composition.

## CLI and external agents

Request the separate planning grant when connecting:

```sh
forge connect https://forge.example --repo owner/repository --triage
forge triage owner/repository --server https://forge.example --request-id triage-2026-10-10 --json
forge planning owner/repository --server https://forge.example --json
forge planning owner/repository --server https://forge.example --proposal PROPOSAL_ID --json
```

Read the returned `next` cursor to fetch another row page with `--after`. Group members have their own `members_next` cursor; use `--row ROW_ID --after CURSOR`. Repository notes and proposal lists are also available through this surface. `forge --json commands` includes `planning_commands`: CLI input schemas, the complete `planning.action_file_schema`, and an operation index for `triage`, `rules`, `note`, `edit`, `choose`, and `accept`. The edit schema describes every row type, including exact existing-PR publication approval. Schema descriptions identify revision pins, pagination cursors, UTF-8 byte limits and replay rules.

Mutation commands take an explicit JSON operation file:

```json
{
  "action": "accept",
  "request_id": "approve-proposal-2026-10-10",
  "proposal_id": "PROPOSAL_ID",
  "expected_version": 2,
  "accept_rows": ["ROW_ID"],
  "reject_rows": []
}
```

```sh
forge planning owner/repository --server https://forge.example --action-file decision.json --json
```

Reuse the same `request_id` for the same intent after a lost response. Requests remain repository-scoped and operator-attributed. Read access uses `repositories:read`; planning requests, rules, notes and acceptance require the separately approved `repositories:triage` capability. Existing agent connections do not acquire it automatically. Personal GitHub credentials stay in the local CLI. The planning worker receives immutable issue facts and read-only Git access through its job-bound mirror; GitHub installation credentials stay outside the worker.

Other operation shapes (all include `request_id`):

- `rules`: `expected_revision`, `rules`, `proposals_enabled`.
- `note`: `note_id`, `expected_revision` (zero for a new note), `kind`, `content`, `sources`, `deleted`. Each source has `url`, `description`, `kind`, `identity`; kinds are `issue_revision`, `candidate`, `commit`, `decision`, or `document`.
- `choose`: `proposal_id`, `row_id`, `expected_version`, `choice_id`, `digest`. Use identities returned by the row's `steward_decision`; this selects an alternative without accepting the row.
- `edit`: `proposal_id`, `row_id`, `expected_version`, `value`. `value` has the displayed `action`, complete `members` issue IDs, `reason`, `rule_quote`, and action-specific `parameters`.

The client transport limits each request to 16 KiB. Read larger proposals through their paged views and accept bounded sets of rows. An individual approved group remains one atomic composition regardless of its member count.

## Persistence and recovery

The store retains normalized proposal inputs, rows, membership, rule/note revisions, acceptance receipts, effect authority and immutable subject decisions. Planning context and captured output use the existing auxiliary artifact storage. Each repository's scheduling watermark is persisted with the immutable worker context. Events arriving after capture stay pending across restarts and worker completion. The planner reuses the existing container runner, credential admission, execution lease and verified cleanup boundary. It cannot approve its own proposal. Process interruption resumes the same owned execution or its captured result; it does not create another runner or reset a delivery's failure budget.

A frozen proposal permits at most three physical worker executions. Once exhausted, it becomes failed with `steward_cycle_limit` and keeps all captures. Use **Triage now**, or `forge triage` with a new request ID, to capture a fresh proposal. Replaying an existing request cannot reset this limit. Existing issue failure charges are unchanged.

## Prerequisites

An approved typed `wait_on` choice records a prerequisite on another captured
backlog issue. A manually edited ordering row can also name explicit
`owner/repository#42` references, including another repository. Issue prose alone
does not establish a dependency. Forge resolves an external target
with its Controller App, pins its repository and GitHub identity, and refreshes
its state before work can proceed.

| Target observation | Result |
| --- | --- |
| Issue closed as completed | Satisfied |
| PR confirmed merged | Satisfied |
| Open issue or PR | `prerequisite_open` |
| Closed, unmerged PR | `prerequisite_closed_unmerged` |
| Issue closed as not planned or duplicate | `prerequisite_not_planned` or `prerequisite_duplicate` |
| Unknown closure provenance | `prerequisite_completion_unknown` |
| Missing App access or unreadable target | `prerequisite_inaccessible` |
| Reference now points to another identity | `prerequisite_identity_changed` |

Internal group edges use the complete set of accepted task contributions for
each prerequisite issue, so the issue need not close before dependent work runs.
The builder's task graph pins those exact outputs. A task spanning both sides of
an approved dependency must explicitly handle that dependency internally;
cycles are rejected before execution. Independent tasks may run while another
prerequisite remains blocked. Issue grouping remains an operator decision; task
allocation does not change the approved membership or publication target.

### Planning diagnostics

Completed worker captures keep structured results permanently. Their scrubbed terminal logs are stored separately, bounded to 64 KiB per execution. After 30 days, verified cleanup and lease release allow the supervisor to expire the log bytes while retaining their digest and expiry receipt. Expiring logs never changes the structured capture digest.

A missing result artifact is reported as `triage_result_missing`; a present result with the wrong dossier identity or decision shape is `triage_result_invalid`. Historical captures also validate their original attempt, role and outcome; a legacy non-pass result is `triage_worker_unsuccessful`. Semantic proposal rejection remains `triage_proposal_invalid`. A missing artifact alone does not identify a provider or harness failure; the retained diagnostic log supplies that evidence when available.

Repository planning results have a 4 MiB limit shared by the installed provider adapters and host capture. Other worker results retain their 64 KiB limit.
