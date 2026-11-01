# Local Forge client

This standalone package connects an external agent to Forge, prepares repositories
using local GitHub credentials, and verifies/enables them through scoped server
operations. See [agent connections](../docs/agent-connections.md) for installation,
supported commands and resumption. `forge skill` prints the bundled agent skill.
Use [fresh-host setup](../docs/cli-fresh-host.md) for the guided installation-to-first-merge
workflow, or its deterministic JSON commands.

## Credential boundary

`GitHub` runs the operator's local `gh api` with fixed github.com endpoints and
structured input. It delegates authentication to `gh` and never asks it to print
a token. Child output is bounded;
errors omit raw output and diagnostics. The subprocess receives local GitHub
credential configuration, while the Forge interface receives only repository
identity, instance/generation, capabilities and configuration revisions.

`Preparation` combines current Forge requirements with GitHub repository state.
Plans have a ten-minute lifetime and pin App/installation IDs, repository identity
and the Forge configuration revision. Applying a plan reconstructs its App access
actions from fresh observations. Roles sharing the Forge App use one installation
grant; each grant adds only the selected repository.

Forge's supervisor enforces CI, independent Review and Audit, and the selected
human/auto policy before its own merges. Setup observes existing GitHub rules
without creating or repairing rulesets, changing bypass actors, or requiring
GitHub Pro. The GitHub merge API still enforces the repository owner's rules.
A merge refused by those rules reports its blocker on the issue.

A failed grant returns a GitHub browser link. Resuming independently refreshes
App access before deciding whether another grant is needed. Calling `setup`
without `apply=True` performs reads only. Activation additionally requires
`enable=True`, an activation capability, and Forge's own readiness checks.
Old journals preserve attempted rule writes as history; resume never replays them.

## Authenticated transport contract

The engine's `forge` argument supplies:

- `authorize(instance, generation, target, capability)`; verify the current grant,
  operator, reset generation and repository scope. Reject before any requested
  effect. Default capabilities are `repositories:read`, `repositories:prepare`,
  and `repositories:activate`. Planning and merge-policy mutations require the
  separately approved `repositories:triage` and `repositories:policy` grants.
- `requirements(target)`; return the closed contract produced by
  `forge.repository_requirements.describe` after authorizing the target.
- `verify(target, revision)`; authorize and schedule independent App access and
  observations of existing GitHub rules. It may return while work continues; requirements then
  indicate incomplete verification.
- `enable(target, revision)`; authorize activation and call the existing
  `RepositorySetup.enable` path, which rechecks server-side readiness and revision.

Every actual server operation must enforce authorization itself. The client's
preflight `authorize` call cannot authorize a later HTTP request by itself.
The transport must filter response schemas, bound request/response sizes and
supply pinned App identities from an approved connection. The requirements
helper is not an authentication gate and must not be exposed unauthenticated.

`Workflow` supplies that interface through the authenticated `Client`, with a
private local journal and durable server operations. Browser action tokens and
the UI upstream secret are not substitutes for client authentication. Forge uses
its own App observations and the existing settings/readiness gates at activation.

## Verification

```sh
python3 -m unittest tests.test_agent_repository_setup tests.test_client_repository_workflow tests.test_client_connections -v
```

The integration fixture runs the real settings/requirements/activation services
with separate mock operator GitHub and Forge App clients. It verifies that a
previously undiscovered repository reaches activation, repeat setup avoids
additional administrative writes, and synthetic operator secrets stay out of
Forge calls, persisted server files and result objects. It also exercises plan
editing, changed identities, expired plans, capability restrictions, revocation,
existing owner rules, interrupted App grants and subprocess failures.

## Repository merge policy

```sh
forge connect https://forge.example --repo OWNER/REPO --policy
forge repo policy OWNER/REPO --server https://forge.example --json
forge repo policy OWNER/REPO auto --server https://forge.example --json
forge repo policy OWNER/REPO human --server https://forge.example --json
```

`--policy` requests the separate `repositories:policy` capability in the browser
approval. Existing default connections can inspect policy but cannot change it.
The default mode is `human` (one counted approval). `auto` still requires CI,
Review and Audit, and cannot bypass explicit human verification or an Audit
request for a person. Settings displays the effective policy read-only.

A write first reads the current revision and uses compare-and-set. Agents can
supply `--revision` and a stable `--request-id` (32 lowercase hexadecimal
characters) to replay the exact same decision after losing a response. A reused
ID with different inputs is rejected. A stale revision requires a fresh read and
new decision; the CLI does not overwrite a concurrent configuration change.
Repeatedly setting the already explicit mode does not create another runtime
configuration revision.

## Repository planning and delivery progress

```sh
forge connect https://forge.example --repo OWNER/REPO --triage
forge triage OWNER/REPO --server https://forge.example --request-id triage-pass-1 --json
forge planning OWNER/REPO --server https://forge.example --json
forge repo delivery OWNER/REPO ISSUE_NUMBER --server https://forge.example --json
```

Planning reads, proposal pages and delivery progress use `repositories:read`.
Triage requests, rule/note edits, proposal acceptance and explicit membership
changes require `repositories:triage`. A planning worker cannot approve its own
proposal. Existing connections do not gain new grants automatically.

`forge planning ... --action-file FILE` accepts explicit typed proposal decisions;
`forge delivery inspect ...` returns the membership-change preview that
`drop`, `close-member`, `dissolve` or `resume-delivery` can approve with `--decision FILE --apply`.
Delivery progress includes canonical member status, all applicable blockers,
active assignment IDs and exact prerequisite/output references. An accepted output
does not clear a member’s independent holds.

The progress read `repo delivery` uses an issue number; membership commands use
the exact issue key returned by `repo issues`. See [repository planning](../docs/repository-planning.md)
and [delivery execution](../docs/delivery-execution.md#change-membership-explicitly)
for paging, identities, preview fields and failure-budget effects.

## Client maintenance and account renewal

`forge version --ssh anvil` reports the client commit and reads the installed
server commit over verified SSH. Different revisions produce a warning.
`forge self-update` selects the latest published prepared release.
`forge self-update --revision COMMIT` uses `uv tool install --reinstall` to install
that exact client revision, then verifies the installed command and revision.
Install uv first and use `uv tool install` for the initial CLI installation, as
shown in the [README](../README.md#install). Existing commands owned by
other package managers are left for you to resolve; Forge does not force replacement.

`forge account login codex --ssh anvil` works during setup and after Start.
For an API key, use `--kind api_key`; the CLI prompts privately. Agents can use
`--kind api_key --secret-stdin --json`. The server still checks operator identity,
setup generation and runtime locks. Account renewal does not reopen App or
repository setup authority on an active factory.

## Saved installation interview

Run `forge install` for the interview, or supply its deterministic inputs:

```sh
forge install --ssh anvil --intent-file install.json --json
forge install --ssh anvil --preflight --json
forge install --ssh anvil --apply --json
forge install --ssh anvil --status --json
forge install --ssh anvil --resume --json
```

`forge commands` includes the closed `installation_commands.intent_schema`.
The intent records multiple repositories, per-repository human/auto merge policy
(default human in the interview), workflow-edit permission, provider login kinds,
operators, App owner, hostname and a source revision
resolved once. It accepts no credentials. Resume preserves the saved inputs and
observes server progress after handoff without replaying bootstrap. Local journals are private and each stage records its
input digest, status, safe receipt and whether a write is still uncertain.

Preflight performs read-only observations. It checks local tools and tailnet,
client revision, owner/repository authority, effective and classic rules,
signatures, workflow files, dependency evidence, existing ready issues, host
prerequisites, resources, TLS egress and Tailscale certificate domains. It prints
`pass`, `will fix automatically` or `needs you`. Apply rechecks the findings and
refuses unresolved prerequisites. Only findings explicitly marked waivable can
be waived with `--waive ID`; host identity and resource checks cannot be waived.
For one worker slot, the minimum is 2 CPUs, 7.5 GiB observed RAM and 20 GiB free
disk; database dependencies raise the disk floor to 40 GiB. Databases run inside
the same 4 GiB worker limit, so the RAM minimum stays 7.5 GiB. Runtime memory
admission still checks available memory before each worker starts.

The coordinator installs the host, sets up private HTTPS, guides registration and
installation of one GitHub App, connects the coding account and qualifies the
repositories and worker environments. It then runs a test issue through Triage,
Build, independent Review and Audit, and a verified merge. Only after that test
passes does it enable the selected repositories with their chosen merge policies.
GitHub approval and coding-account sign-in are attended browser steps; the CLI
handles the server commands, file transfers and saved progress.

After host, private HTTPS, App and operator setup, the CLI submits the approved
preparation plan and opens `/installation`. The server prepares the environment
and checks repositories while the operator connects Codex in the web page.
Human CLI use returns at this handoff; closing it does not stop setup. Agents
can follow through handover with `--json --wait SECONDS`. Use
`forge install --ssh HOST --status --refresh --json` to reconcile server progress
without applying installation; plain `--status` remains offline. GitHub App
registration and repository grants use local administrative access. Forge setup
does not change repository rules.

The private GitHub App must be owned by the account that owns the selected
repositories. For personal repositories, choose your personal GitHub login as
the App owner. Private repositories on GitHub Free are supported without a
protection-mode setting. A private organization App cannot discover personal repositories.

If only preflight has run, revise the saved plan with
`forge install --ssh anvil --replace-plan` and answer the interview again, or
supply `--replace-plan --intent-file install.json --json`. The CLI validates the
new inputs, confirms that the host is uninstalled, and archives the old plan.
Once any installation stage has started, use `--resume`; replacing that plan is
refused because GitHub or host changes may already exist.

## Native worker boundary qualification

After the matching server source and worker image have been staged through the
supported upgrade, run:

```sh
forge setup qualify-boundary --ssh anvil
forge setup qualify-boundary --ssh anvil --apply
```

The first command reads the installed image, source revision and Docker version,
then prints the exact image ID and checks. The second selects that host’s current
installed image and reports its exact identity with the result. Supply
`--image sha256:IMAGE_ID` to select an explicit pinned image; its plan needs no SSH.
Both host operations use the saved SSH host identity
and runs disposable canaries as the Forge service account. It requires a native
Linux host with Docker Engine 28 or newer and the exact worker-boundary v2 image.
It creates its own networks, volumes, endpoints and local Git fixture; it does
not change installation settings, services, repository permissions or jobs.

The receipt verifies real Git clone/fetch and denied push, live session revocation,
provider TLS without credentials, public npm/pip downloads, blocked GitHub/host/
cross-job access, immutable assignment inputs, the work-volume limit, capture
after worker exit, and confirmed cleanup. All checks must pass. A network outage
fails qualification. Local Docker Desktop 23 cannot qualify native isolation;
the separate local volume canary provides only its stated partial checks.

The work volume is bounded tmpfs. Its pages count against the writing worker's
memory limit; the quota is a ceiling, not a memory reservation. The trusted
frontend keeps it mounted through stopped-worker capture. A full host restart
loses these temporary bytes, so recovery must retry the physical job.

## Existing GitHub rules

Forge owns the decision to merge its work after its checks pass. Repository
owners independently manage branch protection and rulesets on GitHub. Those
rules can require additional reviews, CI results, signatures or other conditions.
Forge reports a refused merge and retries when the blocker is resolved.

The repository's Review and Audit scope remains `all` by default, with `forge`
for Forge-created pull requests only. This scope controls Forge's workers; setup
does not install a required GitHub check for it. Human pull requests remain under
their author's control.

`--github-protection`, `rules_repair`, and repository `github_protection` intent
fields are retired. Older saved installation values remain part of their
historical identity so setup can resume, but authorize no GitHub rule changes.
Previously installed Forge rulesets are not silently removed. Their cleanup is
an explicit repository-owner decision.
