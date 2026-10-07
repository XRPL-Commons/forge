# Legacy state import: private staging only

This is the capture and import work for [#209 phase 2](https://github.com/XRPL-Commons/forge/issues/209).
It does **not** activate SQLite or complete the production migration. The captured
production sources now have entity mappings and independent fact checks. The
runtime adapters, full API/UI/CLI view parity and cutover protocol remain required.

## Operator workflow

Use the CLI from the checkout containing these commands, or install that version
of `forge-agent-client`. The wheel includes the Python store/import libraries and
SQL schema; installing it does not install or start a server. Offline import needs
the qualified SQLite version described in [the foundation guide](store-foundation.md).

```sh
forge migrate plan --ssh forge --source /absolute/server/checkout --root /absolute/private/runtime --json
forge migrate prepare --ssh forge --source /absolute/server/checkout --root /absolute/private/runtime --apply --json
forge migrate capture --ssh forge --source /absolute/server/checkout --root /absolute/private/runtime \
  --snapshot /absolute/new/private-snapshot --apply --json
forge migrate import --snapshot /absolute/new/private-snapshot \
  --destination /absolute/new/private-staging --json
forge migrate verify --snapshot /absolute/new/private-snapshot \
  --destination /absolute/new/private-staging --json
```

`plan` reports the source revision, inventory sizes, active services, cleanup
blockers, linked SQLite qualification, available disk space and maintenance effects.
`cutover_blockers` are separate from capture blockers: a host with an older SQLite
library can still produce a coherent source snapshot. They currently cover runtime
qualification only; they are not a declaration that the application can cut over.
`prepare --apply` builds a checksum-pinned private SQLite 3.51.3 runtime, installing
compiler prerequisites through noninteractive `sudo` when necessary. It verifies
WAL, FULL, foreign keys and integrity before publishing the runtime. A repeat
checks the installed library digests and qualification without rebuilding. This
does not change system Python/SQLite, service environment or database selection.
The host plan reports that available runtime separately as `prepared_sqlite`.

`capture --apply` briefly stops the managed
Forge user services/timers, copies the state, hashes and fsyncs the snapshot, then
restores the services. The CLI transfers its temporary capture code and the private
snapshot over SSH; the operator does not copy files or enter a server shell.
It does not replace the running checkout. Both the server diagnostic copy and the
local snapshot remain private. Neither is a JSON rollback backend.

The capture adapter supports the existing user-systemd installation with named
`forge*.service`/`forge*.timer` units and a matching supervisor runtime root. It
rejects uncleaned pipeline/discussion/deployment jobs, setup/reset probes and known
qualification executions, matching user cron entries,
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
- Imported stores have `import_only=true`; lifecycle and configuration mutations reject them. There
  is no activation command, JSON switch-back, retention schedule or service restart
  in the import/verify path.
- Verification checks source round trips, relational integrity, unit versions,
  requirements identities, every historical charge/correction, candidates, jobs,
  event sequences/timestamps and every artifact chunk's bytes and digest.
  It also checks conversations and message pages, operator requests, notification
  delivery receipts, configuration, environment scans, repository access/setup,
  setup probes, deployments, provider cooldowns and learned memory allocations.
  Failure evidence and auxiliary execution inputs are separate artifacts.
  Unknown entity fields are explicit blocking findings.
  `public_view_parity: pending` explicitly distinguishes these checks from the
  future API/UI/CLI projection acceptance gate.

## Production-copy dry run (2026-10-06)

The CLI captured the idle production installation and restored its three active
services. The copy contained 70 files / 69,754,890 bytes. No production data was
rewritten or selected as SQLite.

| Imported fact | Count |
| --- | ---: |
| Repositories, including one without tracked issues | 9 |
| Observed issues | 1,023 |
| Units of one | 112 |
| Jobs | 895 |
| Budget entries | 137 |
| Candidates checked | 87 |
| Events | 3,191 |
| Pipeline artifact chunks checked | 3,994 |
| Conversations / messages | 7 / 17 |
| Operator decisions | 17 |
| Notification events | 8 |
| Memory profiles | 24 |
| Configuration documents | 5 |
| Environment scans | 6 |
| Role access observations / repository access rows | 8 / 157 |
| Repository setup observations | 9 |
| Setup, deployment and qualification executions | 13 |
| Deployment profiles | 8 |
| Operator sessions / request audit entries | 1 / 31 |

Source round-trip differences: **0**. All checked normalized facts matched.
The first dry run exposed two importer errors around completed-unit insertion;
those were corrected and covered by a terminal-history regression test. Merge
receipts occur in both issue evidence and events; both forms are now recognized.

The first import reported 34 unmapped supporting files. Those captured sources
now have mappings; the expanded dry run reports **zero unmapped source files**.
That does not mean their runtime adapters have switched to the database.

Remaining findings prevent activation:

- 13 live units retain `legacy_reconciliation` gates until current ownership,
  GitHub facts and their other gates are checked by the cutover reconciler.
- Seven issue observations contain truncated bodies; do not manufacture complete
  requirements from those truncated strings.
- 88 jobs have no retained completion timestamp. Their imported raw logs are
  pinned rather than given an invented retention deadline. This is a retention
  exception, not a missing-data error.

The expanded database was approximately 102.9 MB including both immutable import
evidence and normalized rows. This is not a final storage benchmark: archive retirement,
public-view parity, measured host queries and normal merged
batch execution remain separate release gates.

The current production service environment links SQLite **3.45.1**. The new CLI
successfully prepared a separate **3.51.3** runtime on that host and verified WAL,
FULL, foreign keys and integrity. Compatible service launchers must select this
qualified runtime during cutover; no running service was changed by preparation.

### Identity preservation

The import retains the original opaque authorization epoch, session expiry and
CSRF state, browser-bound OAuth flow verifier, device-code consumption, grant
revocations, scope and setup-generation fences, and every used refresh-token hash.
The numeric SQLite epoch does not replace those existing fences. Tests cover
revoked, expired, old-epoch and old-generation grants remaining unusable.

The identity-only OAuth secret is staged in a private credential file referenced
by the identity configuration. Publication is atomic and resumable after an
interrupted write. It is never included in a migration report. The full sealed
source evidence remains private too. No credentials are sent to GitHub or any
other service during import or verification.

## Source inventory and next work

The next implementation adds an explicit SQLite worker execution driver and
bounded issue/job reads (see [the foundation guide](store-foundation.md#worker-execution-adapter)).
Launches, capture, fencing and cleanup use row commands; the production supervisor
and public handlers have not selected them. These additions do not change
`import_only`, resolve the captured live-unit findings or permit activation.

See [the writer inventory](design/state-source-inventory.md). Complete phase 2 by
comparing every public view against the
same frozen source, and producing a report with no unexplained differences.
Phases 3–4 then move readers and writers together under a maintenance boundary.
Only that later cutover may select the database. #209 remains open.
