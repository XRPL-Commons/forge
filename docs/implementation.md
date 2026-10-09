# Implementation status

`forge/factory` implements the [durable factory contract](software-factory-spec.md).
It is Forge's only operating mode; the earlier tmux fleet supervisor has been removed.
See [runtime setup](factory-runtime.md) for the exact configuration and host policy.

## Implemented

- Guided three-screen onboarding with GitHub App manifests, staged configuration,
  server-owned readiness checks, and supervisor-applied Start and full reset.
- Reset retains selected SQLite lifecycle and audit history after verified teardown.
  Its journal survives restarts, and old reset intents cannot affect a newer installation.

- Supervisor-owned issue history and assignments, with serialized durable
  reservations before launch and checked ownership invariants on load/save.
- Sequential Build, Review, Audit and merge routing; one three-failure issue budget,
  mandatory bounded investigations, repeat-pattern holds and explicit human retry.
- Exact PR submission metadata, revision-bound verdicts, role App publication,
  shared repository admission and supervisor-owned gates for Forge merge requests.
- Disposable container runners with native Codex policy, external reset, verified
  teardown, stale-execution fencing and bounded evidence capture.
- GitHub App discovery and multiple installations per role, repository-scoped
  execution tokens, explicit activation, and versioned configuration shared with Settings through the selected storage authority.
- A factory view with compact actions, confirmation dialogs, current attention
  separated from historical failures, runner cleanup and issue history.

## Qualification and deployment

The code has synthetic full-pipeline/restart tests and real credential-free runner
and native sandbox canaries. The image policy was tested for successful workspace
writes and denial of a synthetic provider authentication file. Those checks do not
prove an authenticated production workflow against a particular repository.

Production cutover still requires operator-owned App installations/keys, provider
authentication, the named runner host policy/internal network, protected branches,
and a successful setup readiness check. No global sandbox setting is relaxed.
External-agent repository setup uses the operator's local GitHub CLI for
administration; elevated GitHub tokens stay local. Forge receives the scoped
role App credentials and dedicated identity-app secret it needs. The first
adapter handles classic branch protection and conservative
wildcard human CODEOWNERS gates; ruleset-only/merge-queue alternatives are held.

The released standalone client configures sign-in/HTTPS on an existing host and
prepares repositories through scoped connections. It does not include the full
fresh-VM installation wizard proposed in #136; that work is not yet merged.
See [the existing-host CLI prerequisites](cli-server-setup.md#prerequisites).

## Separate staged components

The [security inventory](../SECURITY.md#isolation-status) classifies the
independent observer (`observer/`) and the native-account executor proposal as
staged, outside the live container runner. The observer and its mirror retain
their own provisioning and baseline checks; their rollout remains deferred in
#111 until core qualification is complete. A setup state left in the retired `legacy` phase is read as `setup`, and reset no longer inventories
or stops tmux sessions.

## Selected SQLite runtime

The one-way migration selects one private SQLite database for lifecycle,
configuration, identity and supporting state. The database uses WAL and FULL
durability; transactions, revisions and constraints protect local state. Managed
launchers qualify the SQLite runtime and reject missing or incompatible selected
state. JSON readers and writers are fenced after selection; the original capture
is diagnostic only. See [migration and recovery](store-migration.md).

This is not a distributed claim service. Do not operate separate controller roots
against the same repositories. Long-lived App/provider credentials remain
private referenced files outside the database. Session/token hashes, CSRF values and pending OAuth verifiers are
private database state. Back up the selected database coherently with its
instance metadata and credential references; a live copy of only the main database file omits WAL
state. Do not use the old JSON directory as a current backup.

The supervisor expires eligible raw logs after 30 days in bounded batches, while
retaining active/pinned evidence, events and structured results. Reset retains SQL
history. Logs remain private diagnostic data even after redaction.

GitHub grants are coarser than individual builder actions; role prompts are
cooperative restrictions for those writes, while submission acceptance and
protected-target merge authority are independently checked. See the
[live/staged/retired security inventory](../SECURITY.md#isolation-status).

The storage cutover does not by itself establish #209's latency target, a real
ordinary issue-to-merge qualification, or multi-issue batch delivery. Those remain
tracked acceptance work.
