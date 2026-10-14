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

The timestamp upgrade normalizes existing issue and pull-request observations to
UTC with six fractional digits. It fixes unchanged imported issues being treated
as newer than fresh GitHub reads. The following migration indexes request audit
lookups so their cost does not grow with unrelated retained history.

### Task topology schema upgrade

Migration 033 converts existing child assignments into immutable tasks and exact
member pins. It preserves original attempt manifests, their hashes, accepted
output IDs and retained Git bundles. Pending v1 planning, child and integration
results are interpreted against their original assignment contract; accepting an
old plan creates normalized task rows. New assignments use manifest version 2.
No captured result is rewritten to resemble a new assignment.

Cleanup, result and investigation gates belong to their recorded physical jobs.
They remain visible on every issue in that job's scope, while independent jobs
can continue. Historical gates and charges remain retained.

The supported upgrade stops writers before rebuilding the referenced tables.
Only its dedicated migration connection temporarily disables foreign-key
checking before the transaction. Full integrity and foreign-key checks must pass
before commit; failure rolls back the entire change. Enforcement is restored
before the connection closes. Runtime connections keep enforcement enabled.
Use the normal pinned `forge migrate upgrade --apply` workflow above; no manual
SQL or data export is required.

## Units imported from the JSON runtime

Migration 041 retires what is left of the JSON import. A delivery still waiting
on its `legacy_reconciliation` gate closes as `import_superseded`; its GitHub
issue and labels are left as they are, and discovery admits the open issue again
as a new delivery. Imported history stays in the store as `legacy_event` rows
and shows like any other event. Backups follow the store rules in
[the runtime reference](factory-runtime-reference.md): copy the database
coherently with its instance metadata and referenced credentials, never just the
live main file.
