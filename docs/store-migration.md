# One-way SQLite migration

The CLI prepares, validates and activates the SQLite runtime for an existing
Forge installation. `capture`, `import` and `verify` remain useful for an offline
rehearsal. Only `activate --apply` authorizes the production switch.

## Select the server service account

Remote maintenance runs as the SSH account by default. If SSH connects as root or
an administrator while Forge runs under a separate Unix account, add
`--service-user` to every remote migration command:

```sh
forge migrate plan --ssh anvil --service-user forge \
  --source /home/forge/forge --root /home/forge/.local/share/forge --json
```

The account must already exist and own its home directory. Root can switch
directly; another administrator needs noninteractive `sudo` permission to run
`runuser`. The CLI sets that account's `HOME` and `XDG_RUNTIME_DIR`, so maintenance
inspects its user services and creates private tools, journals and snapshots as
that account. For `prepare --apply`, the SSH administrator installs the fixed
`build-essential` package only when `cc` or `make` is missing, before switching to
the service account to build the private SQLite runtime. This does not grant the
service account additional sudo permissions. Snapshot downloads use the same account. Returned resume commands
retain the option. `import` and `verify` are local operations and do not accept it.
This option selects the existing installation's owner; it does not create an
account, reinstall Forge or authorize activation.

## Activate an existing server

Install the CLI from the same release deployed on the server. Update the managed
server launchers with that release before activation: migration checks the exact
package fingerprint (Python, SQL and provider manifests), the service commands and their qualified SQLite
runtime. Run these commands on your computer; the CLI handles SSH and private files.

```sh
forge migrate plan --ssh forge --source /absolute/server/checkout --root /absolute/private/runtime --json
forge migrate prepare --ssh forge --source /absolute/server/checkout --root /absolute/private/runtime --apply --json
forge migrate activate --ssh forge --source /absolute/server/checkout --root /absolute/private/runtime --apply --json
```

Activation records the original runtime intent and active services in a private
journal before making changes. It pauses Build and merge admission, stops the
API/UI mutation entrypoints and lets existing workers finish verified cleanup.
If workers remain, it returns `status: draining` with their counts. Continue the
same authorized operation with:

```sh
forge migrate resume --ssh forge --source /absolute/server/checkout --root /absolute/private/runtime --json
```

Once drained, the CLI stops the remaining managed writers, captures a fresh sealed
snapshot, imports normalized rows and checks source, relational and public-view
parity. Unexpected source/configuration changes, unqualified launchers, pending
setup/reset operations and unexplained import findings prevent selection.
It then restores the original enabled/merge settings in SQLite, publishes the
private `state/storage` marker atomically and restarts the previously active services.

The result includes the SQLite instance identity, diagnostic-copy location and
remaining reconciliation-hold count. Imported unfinished work retains its
`legacy_reconciliation` gate until fresh evidence authorizes a typed reconciliation;
activation does not erase gates or reset attempt budgets.

The active supervisor uses the recorded CLI activation authority to fetch current
GitHub facts before releasing each migration boundary. Changed candidate inputs
return to Build; an exact remote merge receipt records the completed merge.
Pending imported failures receive one read-only investigation using preserved
assignment diagnostics and current qualified worker settings. Forge does not
invent an original runnable launch or reverse its failure charge. Incomplete or
interrupted older investigations retain their evidence and require an attributed
operator retry decision. Independent holds and exhausted budgets remain enforced.
Pending legacy operator requests keep their original intention and an attributed
interruption receipt. Forge pauses affected work for a fresh operator decision;
it does not replay an old request with newly invented authority.
Truncated cached bodies stay explicitly incomplete until a full issue read; they
cannot authorize admission or merge.

`resume` follows the journal. Before selection it continues the interrupted drain,
copy or import. An incomplete capture is retained separately and captured again
under the writer barrier. After the marker is published it only finishes the SQLite
restart. It never switches back to JSON. A failed step retains the private staging
and journal; use `plan` to inspect the stage and `resume` to continue after correcting
the reported blocker. No shell session or manual file transfer is needed.

### Upgrade an unfinished legacy installation

An older installation may already have operator sign-in and settings while its
setup has never started pipeline work. `plan` reports `setup_phase`,
`setup_operation_pending`, `initial_setup` and a bounded
`initial_setup_blocker` code. This path requires setup phase, no activation/reset
history, no pending operation or uncleaned workers, and an empty validated pipeline
ledger. A missing ledger is accepted only when the legacy initialization records
prove that setup has not initialized it; no replacement JSON ledger is written.

Use the current release CLI and pin that same full release commit:

```sh
forge migrate activate --ssh anvil --service-user forge \
  --source /home/forge/forge --root /home/forge/.local/share/forge \
  --source-revision FULL_RELEASE_COMMIT_SHA --apply --json
```

Run `migrate prepare --apply` first if the plan reports an unqualified SQLite
runtime. The CLI fetches the pinned source locally, verifies forward ancestry and
sends only Git objects to the server. GitHub credentials and local Git configuration
stay on the operator's computer.

The journal pins the original and target commits. With every Forge service
stopped, the CLI seals the existing configuration and identity snapshot, advances
the checkout, and uses the host updater to build and qualify the current runner,
UI and service launchers. These preparations do not rewrite the frozen settings
or start services. The normal importer then preserves identity, sessions, setup
phase and settings and checks parity before selecting SQLite. The updater commits
its new image and host records to SQLite before the original services restart.
The setup remains unfinished and retains its enabled/merge settings; migration
does not press **Start** or approve work.

If interrupted, use the ordinary `migrate resume` command with the same SSH,
service-user, source and root options. It reuses the pinned source and diagnostic
snapshot. A failure before selection keeps services stopped; a failure after
selection finishes only the SQLite configuration commit and restart. This source
bridge is limited to unfinished initial setup; active legacy installations use
the standard activation and explicit importer repair paths above and below.
Changing the pinned release during an initial bridge is not supported; the CLI
rejects `--accept-tool-update` for this path before changing source or state.

### Resume with a corrected importer

If an import bug requires a newer Forge release, install that CLI release locally
and explicitly select its full server commit:

```sh
forge migrate resume --ssh forge --source /absolute/server/checkout --root /absolute/private/runtime \
  --accept-tool-update --source-revision FULL_COMMIT_SHA --json
```

This option is available only before SQLite has been selected. The CLI verifies
the sealed snapshot and paused runtime, checks that the requested commit is a
clean fast-forward with the same package as the local CLI, and updates only the
server source. Services remain stopped. It retains the previous partial database
for diagnosis, records the authorized repair, and imports the same capture again
with the corrected tool. Source and public-view parity must pass before selection.
If interrupted, ordinary `migrate resume` continues the recorded repair.

Do not run the regular server update or edit the journal during this maintenance
window. The repair command preserves the original service list, runtime settings
and capture; it does not recapture state or restore JSON writers.

The pre-migration copy is kept for diagnosis. It contains private state and must
not be attached to issues, PRs or chat. The selected marker and instance identity
are fixed; old launchers and JSON writers must refuse the selected installation.

## Optional offline rehearsal

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

For a `capture` operation, resume restores only the previously active services.
For an authorized `activate` operation, it continues the one-way protocol above.
A failed restoration retains its journal. A partially written capture has no valid
seal and cannot be imported.

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
  is no selection or service restart in the import/verify path; activation uses the
  separate maintenance protocol above. There is no JSON switch-back.
- Verification checks source round trips, relational integrity, unit versions,
  requirements identities, every historical charge/correction, candidates, jobs,
  event sequences/timestamps and every artifact chunk's bytes and digest.
  It also checks conversations and message pages, operator requests, notification
  delivery receipts, configuration, environment scans, repository access/setup,
  setup probes, deployments, provider cooldowns and learned memory allocations.
  Failure evidence and auxiliary execution inputs are separate artifacts.
  Unknown entity fields are explicit blocking findings.
  `public_view_parity: pending` explicitly distinguishes these checks from the
  separate public-view acceptance gate run by activation.

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

The historical dry run left these findings to resolve or preserve at activation:

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

## Source inventory and release validation

The selected runtime composes row-based pipeline controllers and supporting-domain
writers. The migration protocol checks public views against the same frozen source
before selection. Explicitly retained reconciliation holds remain visible in the UI.
See [the writer inventory](design/state-source-inventory.md) and
[the foundation guide](store-foundation.md).

Code and schema changes alter the importer fingerprint. An offline rehearsal uses a
new staging destination after such changes. An interrupted production activation
must resume with its original source/tool identity. Production activation and a real
issue reaching its first merge remain release-validation steps; implementing this
command does not claim that either has already happened. #209 remains open.

## Upgrade an installation that already uses SQLite

Use the local CLI from the release you are installing. This updates the selected
host and database together; normal API reads never apply migrations.

```sh
forge migrate upgrade --ssh forge --source /home/luc/forger-remote \
  --root /home/luc/ops/forge-watchdog --json
forge migrate upgrade --ssh forge --source /home/luc/forger-remote \
  --root /home/luc/ops/forge-watchdog --source-revision FULL_RELEASE_COMMIT \
  --apply --json
```

The first command reports the current and target schemas and any workers still
awaiting cleanup. The second verifies that the CLI bundle matches the selected
release, then temporarily disables new agent work through the versioned runtime
configuration. Existing workers continue to finish and clean up; new merge sends
wait too, while already-sent merge outcomes can still be reconciled. If workers
remain, it returns `status: draining` and a complete `next_command` promptly.
Run that command again when they have finished. The supervisor cannot refill
capacity while the drain setting remains in place.

Once cleanup is complete, the CLI stops managed writers, advances the checkout,
and applies pending migrations in one transaction. It restores the original
`enabled` value before the normal updater restarts services. Restoration changes
only that field and preserves other settings edited during the drain. If an
operator re-enables agents before draining completes, the CLI reports
`drain_configuration_changed` and keeps the newer choice; switch agents off
explicitly before resuming. Identities, issue history and worker budgets remain.

If the connection drops or the update fails, repeat the **same apply
command with the same release SHA**. The private maintenance receipt records
whether the schema committed, so a retry finishes deployment without repeating
the data change. An invalid timestamp or failed integrity check rolls back the
migration transaction; services remain stopped until a corrected forward update
can complete. To install a corrected release, use its CLI and explicitly apply
its new full commit SHA; the command verifies forward ancestry and retains the
services stopped by the earlier attempt. There is no JSON switch-back.

The timestamp upgrade normalizes existing issue and pull-request observations to
UTC with six fractional digits. It fixes unchanged imported issues being treated
as newer than fresh GitHub reads. The following migration indexes request audit
lookups so their cost does not grow with unrelated retained history.
