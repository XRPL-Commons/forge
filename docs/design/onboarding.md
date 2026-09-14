# Forge: fresh setup and reset

Design proposal. [Open the clickable prototype](onboarding.html). It uses synthetic
data and simulates all actions locally; it does not connect to GitHub, handle real
credentials, or stop any services.

## The shortest useful journey

**Connect GitHub → Choose a repository → Connect the coding provider → Start Forge.**

Three screens, one primary action per screen. Start from a fresh installation.
There is no migration wizard, mandatory pilot, issue picker, model-tuning form,
JSON editor, or second activation ceremony in this first experience. Once started,
the normal supervisor picks up the next eligible ready issue and the dashboard
shows its progress. Existing review, audit and merge rules remain in force.

This design assumes Forge has been installed on a host. A prerequisite that is
missing should produce one clear next action; it should not add a permanent setup
screen for every possible failure. Host installation and privileged host changes
remain distinct from connecting accounts in the browser.

## Screen 1: GitHub

**Connect your GitHub workspace**

“You own the Apps and choose which repositories they can access.”

Primary action: **Connect GitHub**.

Forge fills out the App registration details and guides the human through the
required GitHub approvals. Keep the four distinct identities already agreed:
Builder, Reviewer, Auditor, Controller. GitHub still requires separate registration
and installation approval for each App; a single guided flow reduces navigation
and manual entry, not those ownership decisions.

Show a compact role checklist as connections become verified. Remember completed
connections so returning does not restart all four. Receive App IDs and keys through
the manifest flow and store keys privately, with references in configuration. The
operator should not copy installation IDs, PEM files, or environment variable names.
No administrator token is requested.

After all four connections are independently verified, continue to repository
selection. The prototype abstracts the external GitHub registration/install screens.

## Screen 2: Repository

**Where should Forge work?**

Show the repositories available to all four Apps. Choose one to begin with.
Repositories outside that shared access can be fixed on GitHub and refreshed;
they are not silently granted access or enabled.

Primary action: **Continue**.

Selecting a repository saves a draft, not an activation. There is no issue picker:
the normal factory will discover eligible issues labeled ready. More repositories
can be added later in Settings.

## Screen 3: Start

**Ready to put Forge to work?**

Connect the supported coding provider. Prefer an existing supported server
connection when available; otherwise use its supported authentication flow. Do not
introduce a model or credential-format questionnaire into the main journey.

After connection, Forge automatically verifies authentication/model availability,
repository permissions and required protection, and the runner's ability to start,
stop, clean up and become reusable. Read-only GitHub checks and disposable local
checks can run during preparation; disclose any paid provider probe before it runs.
These checks do not start an issue or write to the selected repository.

Show one compact readiness result and a summary of the selected repository.
Use sensible host-qualified defaults for capacity and the existing three-failure
rule. Advanced configuration remains available after setup, in the same readable
files used by agents.

Primary action: **Start Forge**, enabled only when required checks pass.

Place the consequence next to that button: “Forge will pick up ready issues in
example/site, create PRs, and merge them when required checks and approvals pass.”
The clearly labeled Start action is the authorization; do not add a redundant
confirmation modal. Bind it to the selected repository and current verified config.

Success opens the factory dashboard with the next eligible issue visible. If there
are none, the empty state says **Waiting for a ready issue**, links to GitHub issues,
and explains the ready label. No synthetic issue is created automatically.

## Reset route

Provide **Reset Forge** in Settings and during setup, linking to a dedicated route.
The reset screen is the single confirmation; no typed phrase or additional modal.

Headline: **Start fresh**.

Explain the actual scope in four short lines:

- Stop scheduling and all Forge-managed workers.
- Discard unfinished local workspace edits and pending factory work.
- Clear the current setup, repository selections and Forge-owned credential copies.
- Preserve GitHub issues, commits, branches, PRs and App registrations.

Primary action: **Stop and reset Forge**. Secondary action: **Cancel**.

“Everything” means this Forge installation, not unrelated processes on the host.
Shared provider logins or credential files owned outside Forge are not deleted.
Local history is archived outside the new factory's active state; a fresh setup
starts with an empty ledger and no active connections. The UI can offer links to
remove old App registrations on GitHub separately, but does not delete them or any
remote code as part of a local reset. A subsequent setup can create fresh Apps with
new names and credentials.

After confirmation, show one progress view:

**Stop workers → Clean workspaces → Clear setup → Ready to set up again.**

Keep the dashboard/control surface running so this progress remains visible.
Automatically prevent new starts or allocations until reset completes. Only show
**Forge has been reset** after teardown is verified; then offer **Set up Forge**.
If cleanup cannot be confirmed, keep that resource recorded and show the corrective
action instead of presenting a falsely clean factory.

## Minimum implementation contract

Keep setup deliberately simple without weakening the underlying primitives:

1. Draft setup never becomes active configuration merely because it was saved.
2. Readiness comes from verified observations, not browser completion flags.
3. Start is one durable, repeat-safe supervisor intent bound to the verified setup.
4. Reset first fences the installation and every owned execution, then stops and
   removes their resources, then clears active setup. It never clears ownership
   records before teardown is established.
5. A small reset journal survives the data being reset. Interrupted reset resumes
   from it; an old reset cannot kill workers from a later installation generation.
6. After reset, the active factory starts empty. A private archived lifecycle record
   and reset receipt do not influence the new issue queue or failure budgets.

For this pass, build and qualify the happy path and that single reset route. Defer
migration assistance, existing-App import, multi-provider choices, bulk repository
onboarding and elaborate troubleshooting journeys. Necessary prerequisites and
real failures still need a concise, accurate message and next action.

Reference: [GitHub App manifest flow](https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest).
