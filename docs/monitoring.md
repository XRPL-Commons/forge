# Reading the monitoring page

An issue is the unit of work. Pull requests, reviews, and security audits are
supporting evidence for that issue; they are not additional issues in the counts.

## Current work

The three workstations are Build, Review, and Security audit. Detailed cards show
issues being processed. Muted card stacks show waiting issues, with the count
inside the stack. Blue, purple, and teal identify the respective workstations.
Animation follows observed work on a card; waiting and completed cards stay still.
Reduced-motion preferences disable animation.

The repository drawer uses the same issue identity and places supporting PRs and
evidence beneath it. Step labels belong here: Build 1/3, Review 2/3, and Audit 3/3.
Completed is an outcome, not a fourth workstation. A merged PR does not alone prove
that its linked issue is closed.

Working issue counts require a fresh queue and an attributable worker signal.
Worker sessions and working issues can differ while a process starts, stops, or
has ambiguous issue identity. Forge uses explicit issue references from the same
snapshot as the PR queue, then enriches them with collected GitHub evidence. It
does not guess links from similar titles or the mere presence of a builder claim.


Every occupied worker slot remains visible in its workstation, including stalled
workers. Cards show an exact issue assignment when one is recorded; otherwise
identity is explicitly unconfirmed. **Stop worker** sends an interrupt and pauses
that worker generation. **Resume worker** clears that pause and lets normal
execution checks decide whether it can continue. **Release slot** requests the
existing verified cleanup: dirty, unpublished, or still-running work is retained
with the reason displayed. These controls do not close issues or merge PRs.
Requests and results are retained locally and executed by the locked supervisor;
a stale card cannot act on a replacement worker.

## Needs attention

- **CRITICAL:** the current candidate has a recorded audit PASS and requires human
  approval. Review its changes and audit on GitHub, then approve or request changes
  as an authorized human reviewer. Forge's own comments are not human approval.
- **STALLED:** a recorded execution pause can be tied to this issue's audit queue.
  Inspect the reported blocker and worker output. When evidence cannot identify
  the issue safely, the page reports a session-level problem instead.
- Failed audits return the issue to Build. An ordinary audit waiting for a worker
  is not a failed or stalled audit.

Critical approval waits are separate from the audit waiting stack. A worker that
has finished should release its slot after its idle state, checkout, and current
handoff are verified. An approval wait blocks that issue's completion. It does not
reserve a global worker slot indefinitely.

A cooldown deadline means automatic retries may become eligible again; it is not
a promised start time, a provider quota reset, or proof that the underlying cause
has cleared. Normal triage, dependency waiting, and occupied role slots are not
human-review decisions. Actual triage failures should include their own evidence.

## Activity and history

The Activity panel follows the throughput chart. Its compact preview prioritizes
current warnings and recent useful events. Expand it to search loaded worker,
pipeline, supervisor, dispatch, and notification-failure records. Routine capacity
deferrals remain available there without displacing useful progress in the preview.
Unknown timestamps remain explicitly unknown; source labels distinguish a
supervisor log message from a collected GitHub transition.

The chart counts issues opened and completed within collected coverage. Shaded
days lack coverage. Stage transitions were observed between polls; their recorded
timestamp is not necessarily the exact moment the GitHub state changed.

The dashboard refreshes automatically. The activity observer normally samples
every thirty seconds, while richer history collection runs less frequently.
Verified audit-wait receipts let human decisions appear before that slower
collection completes. GitHub remains the source of raw issue, PR, and review data.

## Capacity and recovery

Session limits are ceilings, not utilization targets. No eligible issue, a role
limit, a human decision, or resource pressure can legitimately leave slots unused.
Use current memory and pressure alongside the recorded admission decision; an old
denial does not describe current utilization.

The supervisor can recover a never-dispatched builder startup that was interrupted
before the agent began. This requires an old enough, empty single-shell session,
no dispatch or observed work in that session generation, fresh unclaimed work,
a clean published checkout, and repeated identity checks under the supervisor
lock. Dirty work, typed input, or active child processes prevent that recovery.

A **Retiring** card means the worker has ended but still occupies a slot while
release is being verified. Once a terminal execution is released with an
unresolved result, its preserved-work card stays in Needs attention and no longer
counts as occupied capacity. The card contains the last worker result and saved
commit. Its protected checkout cannot be reused until reconciliation; this does
not hold worker slots for other repositories.

Open dashboards detect a changed page revision and reload their code once. Tabs
opened before revision detection was introduced need one manual reload.
