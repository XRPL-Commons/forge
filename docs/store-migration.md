# SQLite schema upgrades

Forge keeps its state in SQLite. A release that changes the schema is applied
with `forge migrate upgrade`, run from the local CLI of that release. Normal API
reads never apply migrations. The one-way migration from the old JSON runtime has
been completed and its tooling removed; a fresh host starts on SQLite through
`bin/forge setup initialize`, which also repairs the private SQLite runtime.

## Select the server service account

Remote maintenance runs as the SSH account by default. If SSH connects as root or
an administrator while Forge runs under a separate Unix account, add
`--service-user`:

```sh
forge migrate upgrade --ssh anvil --service-user forge \
  --source /home/forge/forge --root /home/forge/.local/share/forge --json
```

The account must already exist and own its home directory. Root can switch
directly; another administrator needs noninteractive `sudo` permission to run
`runuser`. The CLI sets that account's `HOME` and `XDG_RUNTIME_DIR`, so maintenance
inspects its user services and creates private tools and journals as that
account. This does not grant the service account additional sudo permissions.
Returned resume commands retain the option. It selects the existing
installation's owner; it does not create an account or reinstall Forge.

## Upgrade an installation

This updates the selected host and database together.

```sh
forge migrate upgrade --ssh forge --source /home/forge/forge \
  --root /home/forge/.local/share/forge --json
forge migrate upgrade --ssh forge --source /home/forge/forge \
  --root /home/forge/.local/share/forge --source-revision FULL_RELEASE_COMMIT \
  --apply --json
```

The CLI fetches the pinned release using local GitHub access, verifies forward
ancestry, and transfers the target’s complete Git object history over SSH.
Complete ancestry lets an initially shallow installation verify merged releases. GitHub credentials and local Git
configuration stay on your computer. The service account needs no GitHub login.
The host checks the object-pack digest and exact source fingerprint before
pausing work. After checkout succeeds, resuming uses the installed objects and
does not need another GitHub fetch.

The first command reports the current and target schemas and any workers still
awaiting cleanup. The second verifies that the CLI bundle matches the selected
release, then temporarily disables new agent work through the versioned runtime
configuration. Existing workers continue to finish and clean up; new merge sends
wait too, while already-sent merge outcomes can still be reconciled. If workers
remain, it returns `status: draining` and a complete `next_command` promptly.
Run that command again when they have finished. The supervisor cannot refill
capacity while the drain setting remains in place.

Once cleanup is complete, the CLI stops managed writers, advances the checkout,
and applies pending migrations in one transaction. The normal updater converges
services before the CLI restores the original `enabled` value. Restoration changes
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

If another deployment advanced the checkout after the schema committed but
before reconciliation finished, apply the next release through its CLI. Recovery
requires a clean checkout and proves that the interrupted target is an ancestor
of the current checkout, which must itself be an ancestor of the requested
release. It records the superseded target and repeats the normal maintenance
cycle, retaining the original drain ownership and services to restore. Earlier
unfinished stages, divergent source history and changed operator choices still
stop the upgrade; the CLI never treats an advanced checkout as proof of success.

A corrected CLI can also finish one already-fenced setup probe per draining
invocation when the installed schema already matches its target. It verifies the
source release again, uses the recorded probe configuration to confirm resource
removal, and saves cleanup through the normal setup transaction. Active probes
and all other workers remain drain blockers. This repairs a failed setup cleanup
without editing SQL, discarding execution history, or installing code early.

## Schema baseline

The schema is one file, `forge/store/schema/001_baseline.sql`. It replaced the
41 migrations of the first releases (#289) and leaves out the tables and gate
codes nothing reads any more: the JSON import archive, imported operator
requests, the notification outbox, five tables that were never used, and the
`capacity`, `changes_requested` and `merge_conflict` gate codes. New schema
changes are new migrations, starting at `002`; the baseline never changes.

A store built by the 41 old migrations is rebased once, inside the normal
`forge migrate upgrade --apply` transaction. The rebase runs only when the
store's applied history matches the frozen list of those 41 migrations and
their checksums exactly. It drops the dead tables, removes the retired codes,
rewrites the affected trigger and index, and then compares the result with a
new store built from the baseline: tables, columns, constraints, indexes,
triggers, views and registry contents. Only if they are equal does it replace
the history with the single baseline row and run the integrity and foreign-key
checks. Any difference rolls the whole transaction back and leaves the store at
041. Rehearse it first on a copy of the database:

```sh
python3 tools/rehearse_baseline.py /path/to/copy-of-forge.sqlite3
```

The rehearsal works on its own temporary copy, prints the rows that leave with
the dropped tables, the comparison and the integrity checks, and exits 0 only if
the upgrade would succeed.

Imported history stays in the store as `legacy_event` rows and shows like any
other event; the `legacy_reconciliation`, `retry_delay` and `merge_checks` gate
codes stay because resolved imported gates still reference them. Backups follow
the store rules in [the runtime reference](factory-runtime-reference.md): copy
the database coherently with its instance metadata and referenced credentials,
never just the live main file.
