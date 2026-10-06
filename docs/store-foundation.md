# SQLite store foundation

This implements phase 1 of [#209](https://github.com/XRPL-Commons/forge/issues/209),
consolidating [#212](https://github.com/XRPL-Commons/forge/pull/212) and
[#213](https://github.com/XRPL-Commons/forge/pull/213) into one `forge.store` API.
The running supervisor, API, UI and CLI still use their existing stores. Importing
`forge.store` creates no files and selects no backend.

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
- `Queries` provides paged units, event cursors and artifact metadata. Unit lists
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

## Artifacts and retention

`Artifacts.put()` stores an explicitly identified chunk of at most 1 MiB,
compressing it when that saves space. Repeating the same identity and bytes is
idempotent; different bytes conflict. Reads check metadata before loading a BLOB,
bound decompression, and verify the uncompressed length and digest. Normal unit
and event queries do not load artifact bytes.

`Artifacts.prune_raw_logs()` expires one bounded batch of raw log chunks after
30 days. It preserves active work, cleanup and recovery evidence, open units'
current candidate evidence, and explicit durable pins. Callers must use
`Artifacts.pin_in()` in the transaction that records a durable raw-log reference.
Structured results, summaries, launch manifests and all events remain retained.
An expired chunk keeps its metadata and returns `ArtifactExpired`; retries do not
recreate its bytes. No runtime scheduler invokes retention in this foundation.

## Consolidation checklist

| Capability from the two PRs | Combined implementation |
| --- | --- |
| Private identity, migrations, WAL qualification, snapshots and backup | `db.py` and one versioned SQL schema; FULL durability retained |
| Closed codes, wording, scope/release rules and legal state edges | `codes.py`, `status.py`, matching SQL registries and constraint tests |
| Exact candidate evidence, shared failure budget, stale work and cleanup fencing | Typed lifecycle commands; a lease lasts until verified cleanup |
| Ordered issue observations, retry, merge/closure and dissolution | Typed commands with version, evidence and replay checks |
| Applied and rejected decisions | Atomic command journaling, immutable events and full request identity checks |
| Compression and log expiry | Bounded, digest-checked artifact API with durable pins and 30-day raw-log policy |
| Concurrency and crash behavior | Concurrent command tests, transaction rollback tests and killed writer tests |

The consolidation retains one module/schema layout. It does not introduce a
second backend or a generic state setter that bypasses candidate evidence.

## Local development

The linked standard-library SQLite must contain the WAL reset fix: upstream
3.51.3 or later, or the qualified 3.44.6/3.50.7 patch series. Installation of the
runtime library is a later CLI setup/update change. CI builds checksum-pinned
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

## Remaining #209 work

The [approved design](design/state-and-units-of-work.md) remains the release plan.
Label removal and repaired repository readiness resume eligible work; completed
external closure shows done; batches may include existing PRs with approval.
The store supports these rules, but existing runtime adapters must still adopt it.

1. **Import and parity:** coherent capture of every legacy store, resumable import,
   deterministic comparison and a dry run against a private live-state copy.
2. **Supervisor and API/UI/CLI cutover:** replace every relevant reader and writer,
   reconcile external effects, schedule retention, and measure latency on the host.
3. **Units and batches:** migrate existing work without losing candidate evidence
   or budget history; add approved composition, dependency-aware execution,
   integration, member dropping and a normal merged batch PR. Schema support and
   guarded dissolution do not constitute end-to-end batch execution.
4. **Retire JSON:** remove runtime JSON writes and complete CLI update, backup and
   forward-recovery documentation and rehearsals.

Legacy migration is one-way. A pre-migration source copy is kept for diagnosis;
there is no JSON switch-back. This PR performs no live migration or service restart.
#209 stays open until all release criteria pass.
