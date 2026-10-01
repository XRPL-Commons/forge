# Set up Forge

`bin/forge setup` prepares the host and connects coding accounts in the terminal
(see [installation](installation.md)). Then open /onboarding on the trusted private
dashboard address. It has three steps: GitHub, Repository and Start.

1. **GitHub.** Connect GitHub. Enter your organization (or personal account) name. Create and
   install each of the four Apps in the guided GitHub flow. Choose the same
   repositories for every installation. Return to Forge after installation;
   Refresh access verifies connections without granting additional access.
2. **Repository.** Choose one shared repository. This saves a draft, without
   starting work. Then prepare it. Forge checks its default branch using read-only GitHub
   access. If a review setting is missing, download the ruleset, import it on
   GitHub using your own account, then return and press Check setup. Repositories
   that already meet the requirements skip the import step.
3. **Start.** Coding accounts (Codex, Claude Code, Grok Build) are connected from
   the VM terminal; the Coding accounts screen (/settings/harnesses) provides
   copyable commands. Verification checks the connected accounts and isolated
   worker lifecycle; small model requests may incur provider charges. Press
   Verify repository. Recent account and worker evidence
   can be reused for fifteen minutes if credentials and runtime settings have not
   changed. Repository-only rechecks do not repeat those model probes. Press
   Start Forge once verification passes. This explicitly authorizes eligible
   issue processing and policy-gated merges.

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
view shows the four Forge role Apps and a simple list of repositories. Each
repository is Not enabled, Enabled, or Needs attention. Choose Enable to open its
setup checklist, or Resolve to repair an enabled repository. Add repository opens
the same wizard with a searchable repository picker.

The wizard keeps repository access, review rules, and final activation together.
It shows which Apps already have access and provides Manage access or Install App
links for the missing ones. After making changes on GitHub, refresh access in the
wizard. Repositories invisible to all Apps appear after granting at least one App
access and refreshing. App registration happens once in Forge's initial setup;
new repositories reuse those Apps.

Only incomplete steps ask for action. When access and review rules are verified,
Enable automation confirms activation. Checking, downloading rules, or leaving
the page never enables a repository. The server rejects activation with missing,
unknown, or expired checks, including direct settings writes. Initial onboarding
can still store a repository selection as a non-live draft; its Start action has
its own readiness checks.

Check enabled repositories runs read-only checks. Results expire after five
minutes or when the relevant App connections or repository identity change.
Forge rechecks access and protection before starting work. After a rules repair,
Forge releases only holds caused by missing review settings, preserving failure
budgets, explicit holds and human approval waits. Setup details remain available
inside the repository page. Forge manages credentials; operators do not need to
edit identifiers, credential references, or JSON configuration.

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

Open Settings → Repositories and choose Add repository, or Prepare repository
during onboarding. The wizard guides you through Repository, App access, Review
rules and Enable, with Back and Cancel available throughout. Deployment tracking
and test database evidence live on the repository’s details page. The download targets the default branch observed by Forge. It adds
the two review requirements above and a pull-request-only bypass for the verified
controller App. GitHub does not count the read-only Review and Audit Apps toward
its native approval count. Forge verifies their evidence before the controller
merges. Developers' pull requests still need the repository's required approvals.

On GitHub, open the repository's Settings → Rules → Rulesets and choose New
ruleset → Import a ruleset. Select the downloaded JSON, review it and save. If
GitHub's navigation differs, follow [GitHub's import guide](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/creating-rulesets-for-a-repository#importing-prebuilt-rulesets).
If Forge finds an existing “Forge review baseline” ruleset, it links to editing
that ruleset instead. Existing classic branch protection can also satisfy the
baseline without importing anything, provided its review bypass allows the controller. If import is unavailable for your plan or
role, a repository administrator can configure the same review requirements.

Return to Forge and press Check again. Downloading the file or opening GitHub
never counts as a successful check. Review requirements verified confirms only
the observed baseline. A confirmed missing controller bypass prevents readiness.
If GitHub hides the bypass details, Forge asks for an operator review instead of
claiming they were verified. Organization
rules and stronger protections remain in effect, with source links in the details.

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

### Repair an existing controller bypass

Open the rule linked by Forge, add the controller App shown in the warning to
its bypass list, select **For pull requests only**, save, and check again in
Forge. Update every applicable rule that blocks the controller. Importing a
second ruleset cannot repair the first: GitHub combines applicable rules.
Keep unrelated actors and stronger rules. If the rule also contains CI,
CODEOWNERS, or other restrictions, review those before granting a bypass; a
ruleset bypass applies to that entire ruleset. Prefer keeping the Forge review
baseline separate from those restrictions.

For an administrator using the GitHub CLI, these are the equivalent API calls
(use the owner, repository and ruleset ID from Forge's link):

```sh
gh api --hostname github.com repos/OWNER/REPO/rulesets/RULESET_ID \
  --jq '{name,target,enforcement,conditions,rules,bypass_actors}' > reviewed-ruleset.json
```

Review the saved file and add only this entry to its existing `bypass_actors`,
replacing `12345` with the controller App ID shown by Forge:

```json
{"actor_id": 12345, "actor_type": "Integration", "bypass_mode": "pull_request"}
```

If GitHub omitted the bypass list, obtain ruleset administrator access before
editing it. Preserve all existing actors, conditions and rules. Re-read the rule
before submitting if someone else may have changed it. Then apply the reviewed
file and check again in Forge:

```sh
gh api --hostname github.com --method PUT \
  repos/OWNER/REPO/rulesets/RULESET_ID --input reviewed-ruleset.json
```

The GitHub CLI uses local administrator credentials. Forge receives none of them.
See [GitHub's ruleset API](https://docs.github.com/en/rest/repos/rules#update-a-repository-ruleset)
for organization rules and the permissions required to edit them.

## Deployment profile

Once a repository is enabled, Forge reads it once to propose a deployment
profile: how it deploys, and how to tell a running commit apart from the
source. The Repository page shows this as a "Deployment" card — the strategy
and deploy workflow Forge found, a domain box prefilled with its candidate
when it found one, and where the running commit is exposed (an existing
health field, a response header, or not exposed). The domain box is the only
thing you type.

Confirm accepts the profile. Confirming is the only moment it is written to
repository settings and observation begins; nothing before that point reads
the domain or files anything. Reject discards the proposal with a reason;
Forge does not read the repository again until you press Re-read deployment
or its deploy workflows change. Re-read deployment forces a fresh read
regardless.

When Forge finds no way to expose the running commit — no health field, no
response header, but the application does serve HTTP — confirming also files
an issue on the repository, labelled `forge:deployment`. That issue does not
get the ready label: Forge never queues it for Build. Add the ready label
yourself when you want it worked. A profile with no domain still observes
GitHub deployment statuses for the deploy environment; it just cannot
cross-check what is actually running.

## Host installation

`bin/forge setup` installs the host: runner image, isolation policies, egress
network, operator UI and services. `bin/forge update` applies newer releases. See
[installation](installation.md); there are no files to copy by hand.

Guided setup allows up to four Build, two Review, one Audit and one Recovery worker.
These are ceilings: how many actually run depends on available memory (see
[factory runtime](factory-runtime.md)), so a small host needs no tuning.

Connect or reconnect a harness from the VM terminal with
`bin/forge setup login codex --root <runtime>` or
`bin/forge setup login claude --root <runtime>`, then `bin/forge setup check` with
the same root. The UI shows connection status and terminal instructions.
Forge preserves refreshed installation-owned logins across runner cleanup; an
older runner cannot overwrite a newer login. External imported files remain untouched.
See [installation](installation.md) for API keys, model rotation and fresh VM setup.

## First work

After Start, the factory shows current supervisor activity and the next action.
A missing heartbeat is Waiting for the Forge service, not an empty queue. When
checks pass and no issues are queued, open the linked repository and add the
configured ready label to an issue you want Forge to handle. Forge does not
create a demonstration issue automatically.

The Issues page lists open work in enabled repositories, including issues that
have never been queued for Forge. Search or filter by repository and state; use
Completed to find closed work. Opening an issue shows its description, blocker,
prerequisite issue links, pull requests and recent activity inside Forge.

Choose Queue for Forge to request work. Return to backlog removes its ready label;
Pause adds an explicit hold. Release hold requires an operator reason and checks
current prerequisites before continuing. Setup holds lead to repository setup or
Recheck setup once verification passes. These actions preserve the failure budget.
Retry attempt is reserved for failed attempts and explicitly grants another budget.
Stop attempt warns that unfinished workspace changes will be discarded; published
pull requests remain. Active worker output stays available under Activity.

Viewing issues never queues them. Forge verifies each decision against current
GitHub state, access, rules, explicit local dependencies and existing work. Human
assignees, work-in-progress labels and linked external pull requests require review
before starting another attempt. Unavailable or incomplete observations are labeled;
stale rows cannot authorize decisions. Inventory refresh uses the Controller App's
existing access, without repository administration writes.

Earlier pre-first-build review-settings holds are recognized only by their exact
history, with no prior attempts or failures. They can recover after a fresh check;
explicit holds and existing human work remain paused. Completed issues use GitHub's
confirmed merge or closed state. Worker exit alone never counts as completion.

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

Reset stops only Forge's own runner containers and discussion jobs. It does not
look for or stop tmux sessions or other processes, and it does not touch
checkouts that Forge did not provision as disposable runner workspaces. A setup
state recorded by the retired tmux fleet mode (phase `legacy`) is read as a fresh
setup.

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
The production onboarding screen is part of the operator UI, a separate
`forge-ui` service on the tailnet address; `forge-status` serves only the JSON
API on localhost. See docs/ui-deployment.md.
