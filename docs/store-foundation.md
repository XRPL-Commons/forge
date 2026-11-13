# SQLite store foundation

`forge.store` is Forge's SQLite store API. Importing `forge.store` creates no
files and selects no backend.

## Store and commands

- `Store.create(private_root)` explicitly initializes a new, empty private state
  directory. `Store.open(private_root)` requires the database and matching
  instance anchor; missing or incomplete state is an error.
- `read_snapshot()` yields a read-only snapshot; `write()` commits all changes or
  rolls them back. Writers use `BEGIN IMMEDIATE`, WAL and `synchronous=FULL`.
  Connections enforce foreign keys and disable trusted schema execution.
- `backup(destination)` makes a private, verified SQLite backup without replacing
  an existing file. It does not restore or activate the backup.
- `Lifecycle` admits new and previously observed issues, orders GitHub observations,
  manages scoped gates and pause/resume, and reserves build/review/audit jobs.
  Results must match the assignment and exact candidate. Stale work cannot advance
  a unit, consume its budget or release its execution lease.
- Commands also cover existing-PR adoption, candidate invalidation and technical
  rejection, recovery reconciliation, retry with a new budget generation, verified
  cleanup, confirmed merges, closure and guarded batch dissolution.
- `Queries` provides paged units, issues, job history, event cursors and artifact metadata. Unit lists
  and details use the same lifecycle projection, including canonical status text.
  `codes.py` defines the closed vocabulary; tests require the SQL registry and
  legal state transitions to agree with it.

Commands require an actor, idempotency key and expected unit version. Reusing a
key with different arguments fails. Replaying a successful command returns its
original result without repeating events. Semantic rejections during a command
transaction roll back its state changes, then save the original rejection and
one event; replay returns that rejection even if conditions later change. Invalid
input rejected before a transaction and storage failures do not consume a key.

All callers of the mutation API are trusted controller adapters. They must verify
GitHub and process evidence before entering a transaction. A worker cannot open
the database or authorize a transition by supplying its own report.

## Worker execution adapter

`forge.factory.execution_controller.ExecutionController` is an explicit SQLite
caller. It uses the existing container runner and role-scoped GitHub token issuer, without a JSON ledger shim.

- `Lifecycle.reserve_job(..., launch=Launch(...))` saves immutable launch inputs
  and the capacity lease in one transaction. The adapter currently accepts units
  of one for build, review and audit. Imported historical jobs are never relaunched.
- `ExecutionQueries.pending()` reads bounded pages of execution IDs; `job(id)`
  fetches one assignment. Neither reads captured output or the import archive.
- `Executions.progress()` commits launch progress before the next engine effect.
  A process lock serializes engine calls. After interruption, container inspection
  reconciles the existing execution identity before another create or start.
- `Executions.collect()` saves the worker report and logs, then fences execution
  in the same transaction. Engine reset happens afterwards. Only a verified reset
  releases capacity. A failure to save capture leaves the lease and container owned.
- A `result_pending` gate preserves the original delivery phase while GitHub
  validation is outstanding. Cleanup can finish first. The saved assignment fence,
  membership, requirements and budget generations still govern result acceptance;
  an operator pause or changed requirements makes the delayed result stale.
- Lifecycle completion consumes that pending result exactly once. Worker reports
  are claims; a result evaluator must independently check GitHub evidence before
  passing it to a lifecycle command. Tokens never enter the database.

Issue list pages omit bodies and requirements; issue detail loads them explicitly.
Job pages expose metadata and have a separate artifact API. Unit and issue reads
share issue-state rules. A reopened issue no longer inherits its old delivery's
completion, even when a later GitHub response clears `state_reason`. The old unit
retains its merge receipt and remains merged.

Tests exercise lost create/start/reset responses, capture commit failures, operator
decisions during launch, post-cleanup result validation, exclusive engine access,
and a local build/review/audit/merge-confirmation sequence using a fake runner.
They do not establish a real GitHub first merge or full public-view parity.

### Explicit SQLite supervisor

`forge.factory.sqlite_supervisor.SqliteSupervisor` composes bounded cleanup,
ordered GitHub observations, result validation, merge authorization, durable
GitHub effects and dispatch. Import-only stores reject controller execution.

- Observation generations are reserved before remote reads. Issue facts, configured
  hold labels, ownership and repository readiness commit together. New holds fence
  worker authority; repaired readiness releases only the observation-owned gate.
- Dispatch pins a singleton's requirements, target, policy, provider account and
  runner configuration with its lease. Fresh GitHub preparation checks ownership,
  protections and the exact target. Provider credentials stay outside the database;
  launch rows retain references and prevent conflicting refresh writers.
- Captured results are classified only after verified cleanup. Fresh GitHub facts
  establish publication and candidate identity. Technical verdicts use durable
  effect identities before lifecycle acceptance. Environment/provider/decision
  waits open scoped gates without consuming the failure budget. Stale policy or
  assignment results cannot advance work or consume budget.
- Candidate effects commit intent before GitHub requests. Lost responses are
  reconciled through stable review/check identities and fresh PR reads. Merge
  authorization checks current policy, ownership, accepted checks, protections and
  human approvals again immediately before the exact-head request. An HTTP success
  never establishes a completed merge; a matching GitHub observation does.
- Pages preserve scheduling progress across cycles, and the supervisor records a
  bounded heartbeat including per-item error counts. Its process lock prevents
  concurrent cycles. Raw-log retention runs in bounded batches at most hourly;
  structured results, events and pinned recovery evidence remain retained.

Local integration tests compose the real row controllers with simulated GitHub
and worker engines through build, review, audit, human approval and confirmed merge,
including supervisor restarts and failure investigation before retry. They are
not real GitHub or production acceptance.

### Recovery and verification adapters

`RecoveryController` schedules one read-only investigation for each confirmed
failure, including the third charged failure. The failure record, preserved
structured diagnostics and recovery wait commit with that failure. Recovery owns
an immutable link to the failed assignment and uses the normal runner's launch,
capture and cleanup protocol. Its narrowly scoped gate exception permits investigation
at budget exhaustion while preserving operator, ownership and readiness holds.
An incomplete investigation records findings without charging another failure or
scheduling another investigation. Two repeated failure patterns require an
evidence-backed different approach before another ordinary attempt.

A late publication can correct the original build charge only after the controller
independently verifies the original assignment, branch, base, requirements and
policy. Findings alone cannot publish, select a candidate or reset a budget. An
investigation may request exact-candidate human or configured CI verification;
that request cannot pass Review/Audit or refund a failure by itself.

`VerificationController` completes configured CI verification only for the exact
candidate and operator-selected check names/App identities. An explicit human
confirmation records an attributed receipt; a GitHub PR approval is not proof
that the requested verification ran. The typed command records candidate evidence
and resolves its gate in one transaction. Subsequent worker assignments receive
bounded verification receipts. Merge still rechecks CI, and human verification
requires a later authorized GitHub approval. Generic gate resolution cannot bypass
these verification commands.

The supervisor accepts explicit recovery and verification adapters and pages them
before normal dispatch. Its automatic wait stage releases elapsed provider/retry
waits without clearing unrelated holds or changing failure history. Provider
unavailability also records an account cooldown so another unit does not
immediately reuse that unavailable account.

## Artifacts and retention

`Artifacts.put()` stores an explicitly identified chunk of at most 1 MiB,
compressing it when that saves space. Repeating the same identity and bytes is
idempotent; different bytes conflict. Reads check metadata before loading a BLOB,
bound decompression, and verify the uncompressed length and digest. Normal unit
and event queries do not load artifact bytes.

`Artifacts.prune_raw_logs()` expires one bounded batch of raw log chunks after
30 days. It preserves active work, pending-result, cleanup and recovery evidence, open units'
current candidate evidence, and explicit durable pins. Callers must use
`Artifacts.pin_in()` in the transaction that records a durable raw-log reference.
Structured results, summaries, launch manifests and all events remain retained.
An expired chunk keeps its metadata and returns `ArtifactExpired`; retries do not
recreate its bytes. The SQLite supervisor schedules bounded retention.

## Local development

The linked standard-library SQLite must contain the WAL reset fix: upstream
3.51.3 or later, or the qualified 3.44.6/3.50.7 patch series. Installation of the
runtime library is prepared by `bin/forge setup initialize`, and managed launchers
select it. CI builds checksum-pinned
SQLite 3.51.3 and verifies the Python module actually links it.

```sh
python3 -c 'import sqlite3; print(sqlite3.sqlite_version)'
python3 -m unittest discover -s tests -p 'test_store*.py' -v
```

For an isolated experiment, use a temporary private directory. Do not point this
initializer at an existing Forge instance:

```python
from pathlib import Path
from tempfile import TemporaryDirectory
from forge.store.db import Store
from forge.store.queries import Queries

with TemporaryDirectory(prefix="forge-store-") as directory:
    store = Store.create(Path(directory).resolve())
    assert Queries(store).units() == {"items": [], "next_cursor": None}
```

Tests cover constraint ownership, independent member gates, shared capacity,
ordered observations, stale commands and results, rejection replay, concurrent
failure accounting, cleanup, retention, transaction crashes, disk-full rollback
and consistent backups. No test copies live state.

Schema upgrades are described in [the upgrade guide](store-migration.md).
