# Legacy state import: private staging only

This is the capture and core-import slice of [#209 phase 2](https://github.com/XRPL-Commons/forge/issues/209).
It does **not** activate SQLite or complete the production migration. Supporting
domain mappings and public-view parity remain required before cutover.

## Operator workflow

Use the CLI from the checkout containing these commands, or install that version
of `forge-agent-client`. The wheel includes the Python store/import libraries and
SQL schema; installing it does not install or start a server. Offline import needs
the qualified SQLite version described in [the foundation guide](store-foundation.md).

```sh
forge migrate plan --ssh forge --source /absolute/server/checkout --root /absolute/private/runtime --json
forge migrate capture --ssh forge --source /absolute/server/checkout --root /absolute/private/runtime \
  --snapshot /absolute/new/private-snapshot --apply --json
forge migrate import --snapshot /absolute/new/private-snapshot \
  --destination /absolute/new/private-staging --json
forge migrate verify --snapshot /absolute/new/private-snapshot \
  --destination /absolute/new/private-staging --json
```

`plan` reports the source revision, inventory sizes, active services, cleanup
blockers and maintenance effects. `capture --apply` briefly stops the managed
Forge user services/timers, copies the state, hashes and fsyncs the snapshot, then
restores the services. The CLI transfers its temporary capture code and the private
snapshot over SSH; the operator does not copy files or enter a server shell.
It does not replace the running checkout. Both the server diagnostic copy and the
local snapshot remain private. Neither is a JSON rollback backend.

The capture adapter supports the existing user-systemd installation with named
`forge*.service`/`forge*.timer` units and a matching supervisor runtime root. It
rejects uncleaned pipeline/discussion/deployment jobs, matching user cron entries,
and unmanaged processes using the source/runtime paths. Arbitrary manually
scheduled old binaries are outside this launcher boundary and must be removed
before a supported cutover. Capture does not implement admission draining; when
jobs are active it returns a blocker without stopping services.

A journal is written before services stop. If capture loses its process or SSH
session before restoration, run:

```sh
forge migrate resume --ssh forge --source /absolute/server/checkout --root /absolute/private/runtime --json
```

This restores only the previously active services. It does not import or activate
a database. A failed service restoration retains the journal and returns an error.
A partially written capture has no valid seal and cannot be imported.

## Resume and integrity

- Import reads the sealed snapshot, never moving production files. Each file is
  bound to its name, size and SHA-256 digest. Symlinks, missing initialized stores,
  duplicate JSON keys, nonfinite numbers and changed source bytes fail closed.
- Source records are immutable import evidence, split by collection member and
  into chunks no larger than 1 MiB. They are not a runtime compatibility backend.
  Auth/configuration evidence remains private and must never be attached to a PR.
- Rows and cursors commit together in bounded batches. An interrupted import
  resumes in the same destination with the same source manifest and importer
  fingerprint. Repeated successful runs do not duplicate records.
- The fingerprint covers all packaged Python/SQL source. After changing the
  importer, use a new staging destination; it will not silently mix two mappings.
  An incomplete database initializer fails closed and needs a new staging attempt
  or explicit diagnosis; the importer never deletes it as a recovery shortcut.
- Imported stores have `import_only=true`; lifecycle commands reject them. There
  is no activation command, JSON switch-back, retention schedule or service restart
  in the import/verify path.
- Verification checks source round trips, relational integrity, unit versions,
  requirements identities, every historical charge/correction, candidates, jobs,
  event sequences/timestamps and every artifact chunk's bytes and digest.
  `public_view_parity: pending` explicitly distinguishes these checks from the
  future API/UI/CLI projection acceptance gate.

## Initial production-copy dry run (2026-10-06)

The CLI captured the idle production installation and restored its three active
services. The copy contained 70 files / 69,754,890 bytes. No production data was
rewritten or selected as SQLite.

| Imported fact | Count |
| --- | ---: |
| Repositories | 8 |
| Observed issues | 1,023 |
| Units of one | 112 |
| Jobs | 895 |
| Budget entries | 137 |
| Candidates checked | 87 |
| Events | 3,191 |
| Artifact chunks checked | 3,857 |

Source round-trip differences: **0**. All checked normalized facts matched.
The first dry run exposed two importer errors around completed-unit insertion;
those were corrected and covered by a terminal-history regression test. Merge
receipts occur in both issue evidence and events; both forms are now recognized.

Remaining findings prevent activation:

- 34 supporting source files need their domain adapters/mappings, including
  configuration, authentication, setup, discussions, repository observations and
  notifications. Their complete source records are retained as import evidence.
- 13 live units retain `legacy_reconciliation` gates until current ownership,
  GitHub facts and their other gates are checked by the cutover reconciler.
- Seven issue observations contain truncated bodies; do not manufacture complete
  requirements from those truncated strings.
- 88 jobs have no retained completion timestamp. Their imported raw logs are
  pinned rather than given an invented retention deadline. This is a retention
  exception, not a missing-data error.

The database was approximately 98.5 MB including both immutable import evidence
and normalized rows. This is not a final storage benchmark: archive retirement,
all domain mappings, public-view parity, measured host queries and normal merged
batch execution remain separate release gates.

## Source inventory and next work

See [the writer inventory](design/state-source-inventory.md). Complete phase 2 by
adding the remaining domain mappings, comparing every public view against the
same frozen source, and producing a report with no unexplained differences.
Phases 3–4 then move readers and writers together under a maintenance boundary.
Only that later cutover may select the database. #209 remains open.
