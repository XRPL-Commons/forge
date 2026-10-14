# GitHub observation sweeps

The SQLite supervisor walks all current members of nonterminal deliveries in
bounded SQL pages during its observation phase. A page size of 50 limits each database read;
it does not limit the phase to 50 deliveries. Network calls never hold a database
transaction. This remains sequential remote collection; repository batching and
concurrent collection are separate work.

## Budget and continuation

`cycle_budget_seconds` supplies one shared monotonic GitHub deadline. Every App
HTTP request checks the deadline and uses a socket timeout capped by the remaining
allowance and the existing 15-second request limit. An in-flight socket operation
can finish after the deadline; this is not a hard wall-clock deadline for Docker,
local database work, DNS resolution, or the whole supervisor process. The next
GitHub request cannot start after the deadline.

Execution inspection and cleanup always run first. A cycle that spends its remote
allowance saves the following phase as the next cycle's starting point. Later
phases still get a chance to do local work in the exhausted cycle, and retention
still runs. On the next cycle, results, merge admission, effects and dispatch can
receive the fresh allowance before another large observation sweep. A small
workload keeps the existing phase order. Every dispatch and merge retains its
normal fresh admission checks; changing the phase order grants no authority.

Budget exhaustion is a deferral, separate from GitHub unavailability and worker
failure. It cannot charge the failure budget, fence a healthy planner, or publish
a discovery error. An unfinished page is not checkpointed past the pending
request. Execution and operator pages continue their other local items and retain
their cursor when a remote step defers. Token revocation and cleanup verification
are unchanged: if the deadline interrupts revocation after local teardown, the
lease remains held until the next verified cleanup. A verified issue closure or
hold still commits and fences its worker when a following candidate read spends
the allowance; that candidate observation remains pending.

An observation checkpoint identifies the last completed unit, member and
membership generation. A partial group resumes at its pending member. If the
current unit's membership changes, observation restarts against its current
members. A fixed-size digest of active unit/member identities and membership
generations detects additions or recompositions behind the cursor. A changed
scope is reported incomplete, then the next sweep starts from the beginning;
no whole-backlog copy is stored. The checkpoint and sweep receipt commit together.
The former unit-only cursor is replaced by a new sweep on the first upgraded observation phase.

## Durable evidence

Private `store_metadata` records contain bounded receipts:

- `supervisor:observation_sweep`: current and last sweep, plus the last 100 sweep
  summaries and p50/p95 wall and active durations for healthy completed samples.
- `supervisor:observations:cursor`: the matching exact member checkpoint.
- `supervisor:next_phase`: the next phase after budget exhaustion.
- `supervisor:heartbeat`: cycle start/end, phase durations, coded errors, actual
  HTTP attempt/failure totals and the last observed numeric rate-limit headers.

A sweep records pages and member visits, refreshed/deferred/error outcomes, and
actual HTTP attempts including token issuance. A refreshed member has completed
its required issue and ownership/selected-PR reads; it need not be ready to run.
Known negative findings and outstanding operator decisions remain lifecycle
blockers, reported separately from observation completion. Quota headers describe the last
response carrying numeric quota headers, not a combined quota across installations. Receipts contain no request
paths, request bodies, tokens or raw response headers.

At the end of each phase, coverage comes from current active members and their
canonical issue observation timestamps relative to the sweep start. It reports
current unit/member counts, stale or unknown members, stale units and the oldest
successful observation age. These timestamps cover issue facts, not PR or
ownership reads. Moving an already observed issue between deliveries can preserve
valid issue freshness without completing the new member's observation. The
structural scope receipt prevents
a fresh admission or recomposition behind the cursor from producing a falsely
complete sweep.

A healthy complete receipt requires reaching the end, fresh coverage for all
current members, unchanged structural scope, and no failed/deferred visits. A process interrupted before its
checkpoint leaves the sweep marked interrupted; its request totals may be
incomplete and it is excluded from healthy latency samples. Issue freshness alone
does not hide a missing, failed, stale or interrupted candidate/ownership read.

Observation completion does not release execution authority. A grouped member's
observed closure completes observation while its explicit `membership_decision`
remains pending;
a verified external candidate change can be observed while `external_change`
continues to block work. Unknown PR identity or merge state remains an error,
not a successful negative observation. Explicit issue refresh requests finish
once the required observation completes, even when an operator decision remains.

## Remaining qualification

These receipts support measuring the actual workload. They do not establish the
ten-second sweep or single-digit status API targets. A healthy host sweep still
needs measurement, including query/writer time and database/WAL sizes. API timing
is a separate measurement.

Repository planning still requires fresh collection of its complete input before
reservation. If that collection cannot fit one remote allowance, it defers and
needs a separate resumable input-collection contract. The supervisor does not
substitute stale planning inputs to make a budget pass.
