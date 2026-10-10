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
Plans have a ten-minute lifetime and pin App/installation IDs and the exact
repository. Applying a plan reconstructs it from fresh observations. Grant calls
add one repository; rule writes create an additive review baseline. Existing
rulesets are never patched or deleted. A name collision requires independent
verification or operator resolution.

A failed grant returns a GitHub browser link. An uncertain rule write returns
`write_unconfirmed`; the engine does not retry it automatically. Subsequent setup
can verify effective rules before proposing another change. Calling `setup`
without `apply=True` performs reads only. Activation additionally requires
`enable=True`, an activation capability, and Forge's own readiness checks.

## Authenticated transport contract

The engine's `forge` argument supplies:

- `authorize(instance, generation, target, capability)`; verify the current grant,
  operator, reset generation and repository scope. Reject before any requested
  effect. Baseline capabilities are `repositories:read`, `repositories:prepare`,
  and `repositories:activate`. Planning and merge-policy mutations require the
  separately approved `repositories:triage` and `repositories:policy` grants.
- `requirements(target)`; return the closed contract produced by
  `forge.repository_requirements.describe` after authorizing the target.
- `verify(target, revision)`; authorize and schedule independent App access and
  protection checks. It may return while work continues; requirements then
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
stronger rules, collisions, uncertain writes and subprocess failures.

## Print review-rule fixes

```sh
forge repo fix-rules OWNER/REPO --server https://forge.example
forge repo fix-rules OWNER/REPO --server https://forge.example --json
```

This command reads Forge's latest repository observation and prints the same
fixes as Review rules in the UI. It does not run them. A repository or organization
administrator runs the printed `gh api` command locally, then chooses **Check
again** in Forge. Ruleset fixes also require `jq`; they fetch the current ruleset
before preserving its rules, conditions and existing bypass actors and adding the
controller's pull-request bypass. Hidden bypass lists require manual steps.
Administrative credentials remain with local `gh` and are never sent to Forge.
The [GitHub ruleset API](https://docs.github.com/en/rest/repos/rules#update-a-repository-ruleset)
and [organization ruleset API](https://docs.github.com/en/rest/orgs/rules#update-an-organization-repository-ruleset)
describe the write permissions needed by the operator.


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
