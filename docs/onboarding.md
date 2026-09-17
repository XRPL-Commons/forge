# Set up Forge

Open /onboarding on the trusted private dashboard address.

1. Connect GitHub. Enter your organization (or personal account) name. Create and
   install each of the four Apps in the guided GitHub flow. Choose the same
   repositories for every installation. Return to Forge after installation;
   Refresh access verifies connections without granting additional access.
2. Choose one shared repository. This saves a draft, without starting work.
3. Connect Codex and/or Claude Code from `bin/forge setup` on the VM. Terminal
   verification checks the connected accounts and isolated worker lifecycle.
4. On the Start page, press Verify repository. Forge checks GitHub protection,
   coding accounts and worker cleanup. If protection fails, the page lists the
   specific unmet requirements and links to the repository settings. A check
   marked “Not run in this attempt” was skipped because an earlier check failed;
   it does not invalidate an account verified previously. Small model requests
   may incur provider charges. Press Start Forge once verification passes.

You own the Apps. Forge does not request a personal administrator token. GitHub
requires a human to approve each App registration and installation. App private
keys arrive through GitHub's manifest exchange and are stored as private files in
credentials/, with references in configuration. Installation IDs supplied by a
browser are never accepted as proof of access.

Forge polls GitHub rather than receiving webhooks. Its App manifest includes the
webhook URL required by GitHub's schema, with delivery disabled and no subscribed
events. If registration fails, return to /onboarding and press Connect again;
reopening the GitHub registration URL alone does not resend the manifest form.

## Maintain GitHub access

Open GitHub access from onboarding or settings (/settings/github). This operator
view shows the four Forge role Apps, their identities, installation links, and a
searchable repository-by-role access matrix. It is available before setup finishes.
An App registration alone is not verified installation access. Use Install App or
Manage access on GitHub, then Refresh access in Forge to verify the result.

The matrix includes repositories discovered through at least one connected role
or explicitly configured in Forge. Repositories invisible to every role are not
discoverable here. Missing, stale, or unknown access never enables new work. During
onboarding, complete setup before enabling automation; afterward the same page
supports enabling or disabling repositories. Forge manages App identities and
credentials through guided setup; the operator UI does not expose credential
references, manual App identifiers, or JSON editors. Factory settings provides
worker-capacity controls. Help appears in blue callouts, separate from status.

All four installations need access to the same selected repositories. GitHub also
provides an API for adding individual repository grants, but that endpoint requires
a classic personal access token with repo scope and repository admin access; App
tokens and fine-grained PATs cannot grant it. Forge does not store an administrator
PAT or grant itself additional repository access.

## GitHub repository protection

An administrator must configure the default branch before Start can pass. Forge's
Controller has read-only Administration access and cannot configure its own gates.
Forge shares repositories with developers. The onboarding baseline is:

- Require pull requests and at least two approving reviews.
- Dismiss stale approvals when new commits are pushed.

Developers keep their existing branch, pull request and merge access. Repository
owners choose CI checks, push restrictions, administrator enforcement and bypass
permissions. Forge does not require exclusive Controller access, globally required
Forge checks, or up-to-date branches as an installation prerequisite.

Open Settings → Branches and edit the default-branch rule if either baseline
setting is missing. See [GitHub's branch protection guide](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/managing-a-branch-protection-rule).

Forge still publishes its review, audit and merge-policy checks on its own PRs.
Its supervisor requires current evidence from the assigned Reviewer and Auditor
Apps before requesting a merge. GitHub applies the repository's configured rules
at merge time. Stronger repository-wide enforcement is an owner choice; making
Forge checks globally required also affects developers' PRs.

When an auditor asks for human approval, the merge gate stays closed until the
required human evidence for the current candidate exists. The repository's human
CODEOWNERS protection must be configured for that gate; see factory-runtime.md.
Changing code invalidates the earlier review and audit evidence.

Forge reads classic branch protection and active rulesets, including inherited
organization rules. The same review baseline is checked during setup, dispatch
and merge. Unavailable observations never count as passing. GitHub may hide
bypass permissions: review those on GitHub yourself. A baseline pass does not
certify bypass permissions or waive additional repository rules.

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

Connect or reconnect either harness from the VM terminal with
`bin/forge setup login codex --root <runtime>` or
`bin/forge setup login claude --root <runtime>`, then `bin/forge setup check` with
the same root. The UI shows connection status and terminal instructions.
Forge preserves refreshed installation-owned logins across runner cleanup; an
older runner cannot overwrite a newer login. External imported files remain untouched.
See [installation](installation.md) for API keys, model rotation and fresh VM setup.

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
for repositories and worker capacity are available in /settings. Other runtime
configuration remains in the corresponding JSON files.
state/onboarding/ holds the setup and reset journal. Do not delete ownership
records to bypass an unsuccessful cleanup.

If App registration succeeds but its one-use key exchange is lost, remove that
unused App on GitHub and reconnect the role. A stale callback cannot install an
App into a later setup. If access changes or checks expire, refresh and check
again. Setup checks expire after five minutes; Start independently rechecks
repository access, protection and runner prerequisites.

The design prototype in docs/design/ remains a simulation for design review.
The production page is legacy/onboarding.html and uses the supervisor APIs.
