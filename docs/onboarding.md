# Set up Forge

Open /onboarding on the trusted private dashboard address.

1. Connect GitHub. Enter your organization (or personal account) name. Create and
   install each of the four Apps in the guided GitHub flow. Choose the same
   repositories for every installation. Return to Forge after installation;
   Refresh access verifies connections without granting additional access.
2. Choose one shared repository. This saves a draft, without starting work.
3. Connect Codex using an existing server login or an OpenAI API key. Forge runs
   one small provider request, checks GitHub protection, and verifies isolated
   runner startup, forced termination, cleanup, and reuse. Provider usage charges
   may apply. Press Start Forge once the checks pass.

You own the Apps. Forge does not request a personal administrator token. GitHub
requires a human to approve each App registration and installation. App private
keys arrive through GitHub's manifest exchange and are stored as private files in
credentials/, with references in configuration. Installation IDs supplied by a
browser are never accepted as proof of access.

## GitHub repository protection

An administrator must configure the default branch before Start can pass. Forge's
Controller has read-only Administration access and cannot configure its own gates.
Use classic branch protection with these settings:

- Require pull requests and two approving reviews; dismiss stale approvals.
- Require branches to be up to date before merging.
- Require forge/review from the Reviewer App, forge/audit from the Auditor App,
  and forge/merge-policy from the Controller App. Bind each check to its App ID.
- Restrict pushes to the Controller App only, with no users or teams in that list.
- Enforce protection for administrators, prohibit force pushes, and allow no
  review bypass actors.

When an auditor asks for human approval, the merge gate stays closed until the
required human evidence for the current candidate exists. The repository's human
CODEOWNERS protection must be configured for that gate; see factory-runtime.md.
Changing code invalidates the earlier review and audit evidence.

GitHub may require API configuration for expected App IDs before the named checks
have run for the first time. A repository administrator can configure these using
GitHub's branch-protection API. Forge does not post fake successful checks to
populate GitHub's picker. Rulesets-only configurations are not supported in this
first setup path.

## Host installation

The browser assumes the host is installed. See factory-runtime.md for the pinned
runner image, native sandbox policies and internal egress network. Use Linux,
Python 3.10+, and a Docker engine accessible only to the trusted controller.

Keep source separate from private runtime data. Copy the example service units,
replace their paths, and run both the dashboard and supervisor. The stable
supervisor command is:

    /path/to/forge/bin/forge service --root /path/to/private-runtime

The dashboard needs FORGE_ROOT pointing to that same private runtime. Set
FORGE_BIND_ADDRESS to its trusted private IP. It accepts requests at the exact
bound IP and port; arbitrary reverse-proxy hostnames are not supported.

Before setup, copy examples/host-runtime.json.example to host-runtime.json in the
private runtime and set the pinned image ID. This file describes prepared host
infrastructure, so reset preserves it. Put the reviewed seccomp JSON at
runner-seccomp.json in that runtime, or set FORGE_RUNNER_SECCOMP to its absolute
path in the supervisor service. Install the dedicated AppArmor profile and start
the egress proxy as documented in factory-runtime.md. No global sandbox bypass is
used. Run the real container and native sandbox canaries before accepting jobs.

The initial capacity is one running 4 GiB container, shared across roles, with
two Builder slots and one each for Review, Audit and Recovery. Increase concurrency
only after qualifying the host's available memory. Slot counts do not override
the shared running limit. Setup probes use the same resource and isolation policy.

For existing Codex login, set FORGE_CODEX_AUTH_FILE on the dashboard service, or
sign in under its service account. Otherwise enter an API key in Connect Codex.
Forge copies authentication privately; it never removes an external login file.
Provider authentication refreshed inside a disposable runner is not synchronized
back to the external file. Reconnect if a copied login expires.

## Reset and restart

Settings → Reset Forge opens the single confirmation screen. Stop and reset Forge
fences scheduling first. The supervisor stops each recorded container and verifies
its removal, archives lifecycle history, and clears active configuration and
Forge-owned credential copies. It preserves GitHub issues, commits, branches,
PRs and App registrations, shared external credentials, and unrelated host work.

The reset journal is outside the archived lifecycle. It survives service restarts;
an unverified teardown keeps reset pending and scheduling closed. The dashboard
remains available throughout. Old reset intents cannot target a newer setup.
Archives remain private under archives/reset-<operation-id>/; they do not feed the
new queue or failure budget. Remove old App registrations on GitHub separately.

Installing the update on a legacy fleet preserves its workers. The setup screen
offers Reset existing factory before fresh setup. Reset can stop legacy workers
only when each terminal has a verifiable, dedicated systemd scope. If ownership
is ambiguous, reset stays pending for operator reconciliation. Legacy auxiliary timers belonging to this runtime are disabled during reset; the
new supervisor replaces their scheduling responsibilities. Legacy checkouts
are preserved because they were not provisioned as disposable runner workspaces.

## Configuration and recovery

setup/factory.json and setup/runtime.json are draft configuration. factory.json
and runtime.json become active only when the supervisor applies Start. Settings
remain editable through /settings/advanced and the corresponding JSON files.
state/onboarding/ holds the setup and reset journal. Do not delete ownership
records to bypass an unsuccessful cleanup.

If App registration succeeds but its one-use key exchange is lost, remove that
unused App on GitHub and reconnect the role. A stale callback cannot install an
App into a later setup. If access changes or checks expire, refresh and check
again. Setup checks expire after five minutes; Start independently rechecks
repository access, protection and runner prerequisites.

The design prototype in docs/design/ remains a simulation for design review.
The production page is legacy/onboarding.html and uses the supervisor APIs.
