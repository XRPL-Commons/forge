# Implementation status

`forge/factory` implements the [durable factory contract](software-factory-spec.md).
It is Forge's only operating mode; the earlier tmux fleet supervisor has been removed.
See [runtime setup](factory-runtime.md) for the exact configuration and host policy.

## Implemented

- Guided three-screen onboarding with GitHub App manifests, staged configuration,
  server-owned readiness checks, and supervisor-applied Start and full reset.
- Reset archives lifecycle history only after verified teardown. Its journal
  survives restarts, and old reset intents cannot affect a newer installation.

- Supervisor-owned issue history and assignments, with serialized durable
  reservations before launch and checked ownership invariants on load/save.
- Sequential Build, Review, Audit and merge routing; one three-failure issue budget,
  mandatory bounded investigations, repeat-pattern holds and explicit human retry.
- Exact PR submission metadata, revision-bound verdicts, role App publication,
  shared repository admission and supervisor-owned gates for Forge merge requests.
- Disposable container runners with native Codex policy, external reset, verified
  teardown, stale-execution fencing and bounded evidence capture.
- GitHub App discovery and multiple installations per role, repository-scoped
  execution tokens, explicit activation, and configuration files shared with Settings.
- A factory view with compact actions, confirmation dialogs, current attention
  separated from historical failures, runner cleanup and issue history.

## Qualification and deployment

The code has synthetic full-pipeline/restart tests and real credential-free runner
and native sandbox canaries. The image policy was tested for successful workspace
writes and denial of a synthetic provider authentication file. Those checks do not
prove an authenticated production workflow against a particular repository.

Production cutover still requires operator-owned App installations/keys, provider
authentication, the named runner host policy/internal network, protected branches,
and a successful setup readiness check. No global sandbox setting is relaxed, and no administrator credential is used to configure
GitHub. The first adapter handles classic branch protection and conservative
wildcard human CODEOWNERS gates; ruleset-only/merge-queue alternatives are held.

## Separate staged components

The independent observer (`observer/`) and its mirror retain their own provisioning
and baseline checks; the factory does not claim those are deployed. A setup state
left in the retired `legacy` phase is read as `setup`, and reset no longer inventories
or stops tmux sessions.

The factory ledger is local JSON with atomic fsync/replace and invariants, not a
distributed claim service. Do not operate separate controller roots against the same
repositories. GitHub grants are coarser than individual builder actions; role prompts
are cooperative restrictions for those writes, while submission acceptance and
protected-target merge authority are independently checked. History/evidence remains
private runtime data and has no automatic retention deletion in this version.
