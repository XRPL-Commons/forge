# Issue workflow and worker slots

Forge keeps issue progress and execution ownership in `state/workflow.sqlite3`.
An issue is the unit of pipeline work: queued → build → review → audit → done.
Review and audit rejection return it to Build. Three rejected build cycles enter
`failed`; the durable policy and operator retry are defined in [issue-state-machine.md](issue-state-machine.md). A stage also has a disposition:
`pending`, `working`, `critical` (human approval), or `stalled` (recorded blocker).
An idle process, a merged PR, or a released worker does not by itself complete an
issue. GitHub must report the issue closed as completed.

Three records answer different operator questions:

- **Issue:** what is moving through the pipeline, and which step needs attention?
- **Assignment:** which exact issue, or PR revision and linked issues, may this
  execution work on? It records the provider, tmux generation, attempt, first
  verified submission time, and available execution/thread identity.
- **Slot:** does this configured worker position consume capacity? Slots are
  `free`, `reserved`, `starting`, `working`, `retiring`, or `blocked`. Every state
  except `free` consumes capacity, including a reservation without a process.

`submitted_at` proves delivery, not actual execution or completion. A matching
activity observation establishes working state. A completed Codex turn is an
execution result; publication and release still require separate verification.
Provider migration preserves the assignment and target, increments its attempt,
and clears its old generation/provider for a replacement. Its original submission
history remains intact.

## Authority and enabling

GitHub remains authoritative for issue state, labels, PR head/base revisions,
reviews, and security-audit content. Fresh local snapshots and verified canonical
PR links feed the workflow ledger. Local assignments, slots, and their transition
history are authoritative for new dispatches. Dashboard reads do not query GitHub
or change that authority.

`workflow.initialized` records that the database has existed. A missing database
with this marker is an error, not an empty installation. `workflow.enabled`
separately enables supervisor assignment enforcement. Creating a database alone
is not the same as enabling dispatch integration.

During a controlled deployment, with the supervisor paused and its singleton
lock held, bootstrap imports current facts and observes existing workers:

```sh
python3 -m forge.workflow_control bootstrap --state STATE --config CONFIG
```

Workers already running at bootstrap retain their exact observed generation and
are labeled **started before assignment tracking**. Forge does not invent an
assignment from the repository name, a sole issue, or a historical queue. They
may finish existing work; verified release frees their slots before a new tracked
assignment is admitted. Do not repeatedly bootstrap to turn an unexpected worker
into a trusted legacy worker.

History starts from observed facts. The observability collector defaults to a
recent 30-day initial cutoff; older activity or reviews may be unavailable until
collected. Workflow events describe changes recorded since initialization, not a
reconstructed history of every earlier execution. Missing history is not evidence
that an issue completed or that a slot is free.

## Dispatch, uncertainty, and release

A reservation identifies one eligible target and snapshot revision. Before input
is sent, Forge checks the current assignment, exact tmux generation, provider
prompt, and target eligibility. It records uncertain delivery before sending and
confirms submission afterward. A timeout or partial send cannot authorize a blind
retry. A builder uses the assignment-aware `forge next` result, rather than
choosing another issue from the repository queue.

Cleanup requires fresh publication evidence, a verified empty prompt, no child
work, an unchanged generation, and the supervisor lock. Dirty or unpublished work
is preserved. Provider handover additionally records the existing branch, claims,
and changed files so the replacement can resume them.

The release bridge writes a private intent before killing the exact verified
session. Only a successful kill creates a confirmed receipt. Retirement then
updates the assignment and frees its slot in one database transaction; it never
marks the issue done. Failed-start cancellation is limited to a verified empty
startup shell and a reservation that was never submitted. A separate verified
unstarted-attempt transition can cancel a replacement reserved after a recorded
provider migration only when no worker exists and its target is no longer
eligible. That cancellation preserves the earlier submission history; it does
not reclassify previously executed work as never dispatched.

If ledger bookkeeping fails after a successful kill, the release outcome remains
successful and the confirmed receipt waits in `state/workflow-releases/`.
Reconciliation retries it before further reservation. A prepared-only receipt is
uncertain and cannot automatically free a slot. A delayed receipt cannot clear a
replacement generation. Investigate the recorded reason and worker output; do
not delete a reservation or ignore a blocker to make capacity appear available.

## Read state and history

The dashboard exposes local, read-only state and paginated events:

```text
GET /api/workflow
GET /api/workflow?repo=OWNER/REPO&after=0&limit=100
GET /api/workflow?repo=OWNER/REPO&recent=1&limit=100
```

Responses include issues, assignments, slots, repository revisions, events,
`next_after`, and `has_more`. Use `recent=1` for newest-first history and its
`next_before` cursor as `before=N` to retrieve older events; do not combine
`recent=1` with `after`. Repository access follows the enabled configuration.
An unavailable ledger is reported as unavailable rather than zero occupancy.

Python callers can use `forge.workflow.read_state(state, repo=None)` and
`forge.workflow.history(state, repo=None, after=0, limit=100)`. Event IDs provide
pagination; `source_event_id` makes retried mutations idempotent. The engine writes
each authoritative transition and its event/outbox record in one transaction.
The history outbox is separate from the release bridge's confirmed receipts.

## Backup and recovery

Keep workflow databases, markers, private receipts, and worker logs outside the
public source repository. Back up the database with SQLite's backup API while
running, or pause the supervisor and every state writer before taking a consistent
filesystem backup. A raw copy of an active SQLite database alone may omit committed
WAL data. Preserve `workflow.initialized`, `workflow.enabled`, the release receipts,
provider-handover manifests, and related snapshot/activity evidence with the backup.

For recovery, pause admission and hold the supervisor lock. Preserve the broken
files and error evidence; validate a backup with SQLite `PRAGMA integrity_check`
and restore a consistent state set. Reconcile that history against current GitHub
facts and actual tmux generations before admitting work. A stale backup must not
be used to free a worker that still exists. If a confirmed release cannot replay,
inspect its assignment/generation and the recorded refusal before repairing it.

Never delete the database or initialization marker, edit rows, or expire leases
by age to free slots. Restore evidence and use verified transitions. Unknown
ownership remains blocked until it can be established safely.

## Terminal executions and unresolved work

Execution completion and issue completion are separate facts. A finished provider
process does not need a slot merely because its PR handoff cannot be verified.
The supervisor first checks the role-specific published result. Reviewer records
are selected independently of subsequent audit records; an audit rejection does
not invalidate an earlier completed review.

If the task outcome remains unresolved, a terminal Codex execution can retire
through a different proof: its exact generation and execution receipt must match,
it must have ended at least 60 seconds ago, and fresh process-session inventories
must show no live children. The shell must be empty. Two matching checks run under
the supervisor lock before the exact session is removed.

Before removal, Forge records the assignment (or explicitly unknown legacy
identity), branch, commit, file-state hashes, last worker message, and cleanup
reason in a private immutable checkpoint. Dirty and untracked files are retained
in place. A durable quarantine blocks reuse of that resolved checkout, including
other roles configured to share it. Only the execution retires; issue state,
GitHub labels, reviews, and approvals are unchanged. Other checkouts can use the
freed capacity. A prepared but unconfirmed release remains protected.

This is a safety boundary, not automatic retry authorization. Quarantined work
requires explicit reconciliation before reuse; the dashboard shows its concrete
worker result separately from occupied slots. It must never turn back into an
unbounded loop merely because another supervisor pass begins. The current
fallback is specific to terminal Codex executions; other providers retain their
existing verified handoff behavior.
