# SQLite store foundation

This is the first implementation slice of [#209](https://github.com/XRPL-Commons/forge/issues/209).
The running supervisor, API, UI and CLI still use their existing stores. Importing
`forge.store` creates no files and selects no backend.

## What is available

- `Store.create(private_root)` explicitly initializes a new, empty private state
  directory. `Store.open(private_root)` requires the database and matching
  instance anchor; missing or incomplete state is an error, never a new database.
- `read_snapshot()` yields a read-only snapshot; `write()` commits all changes or
  rolls them back. Writers use `BEGIN IMMEDIATE`, WAL and `synchronous=FULL`.
- `backup(destination)` makes a private, verified SQLite backup without replacing
  an existing file. It does not restore or activate the backup.
- `Lifecycle` exposes admission, scoped gates, pause/resume, build/review/audit
  reservation and result acceptance, budget correction and verified cleanup.
  Commands require an actor, idempotency key and expected unit version. Reusing
  a key with different arguments fails; replaying the same command returns its
  original result without repeating events.
- `Queries` provides paged units, event cursors and artifact metadata. Unit lists
  and details share the same lifecycle projection. Output bytes are loaded only
  by an explicit artifact request, in checked chunks of at most 1 MiB.

The schema covers the core lifecycle and supporting runtime domains. Supporting
tables are not yet wired to their existing writers. The transition API implements
the unit-of-one build → review → audit path; it does not yet compose batches,
orchestrate recovery workers, confirm external merges, or migrate live state.

All callers of the mutation API are trusted controller adapters. They must verify
GitHub and process evidence before entering a transaction. A worker cannot open
the database or authorize a transition by supplying its own report.

## Local development

The linked standard-library SQLite must contain the WAL reset fix: upstream
3.51.3 or later, or the qualified 3.44.6/3.50.7 patch series. Installation of the
runtime library is a later CLI setup/update change. CI builds checksum-pinned
SQLite 3.51.3 and verifies the Python module actually links it.

```sh
python3 -c 'import sqlite3; print(sqlite3.sqlite_version)'
python3 -m unittest tests.test_store_schema tests.test_store_db tests.test_store_transitions tests.test_store_queries -v
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
stale commands and results, concurrent failure accounting, cleanup, transaction
crashes, disk-full rollback and consistent backups. No test copies live state.

## Approved policy and next phases

The [design](design/state-and-units-of-work.md) records the operator's decisions:
label removal and repaired repository readiness resume eligible work; external
completed closure shows done; logs last 30 days and events are retained; batches
may include existing PRs with approval; durability is FULL.

Legacy migration is one-way. A pre-migration source copy is kept for diagnosis;
there is no JSON switch-back. The foundation has no importer, retention job,
backend selector or service restart. Next work adds the remaining typed
transitions, then coherent capture, resumable import and deterministic parity
before any runtime adapter switches to SQLite.
