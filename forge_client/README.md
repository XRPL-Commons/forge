# Local Forge client foundation

This package implements the repository preparation engine for #122. It is a
library under development; a distributable CLI, authenticated HTTPS transport,
client connection UI and agent skill are subsequent integration work. No public
operator API or new dashboard authorization path is enabled by this change.
Fresh-host installation is tracked in #136.

## Credential boundary

`GitHub` runs the operator's local `gh api` with fixed github.com endpoints and
structured input. It never obtains a token string. Child output is bounded;
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
  effect. Capabilities are `repositories:read`, `repositories:prepare`, and
  `repositories:activate`.
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

Operator sign-in (#15), TLS, connection approval/revocation, server operation
journaling, local credential storage and command packaging remain prerequisites
for enabling this workflow against a live remote instance. Browser action tokens
and the UI upstream secret are not substitutes for client authentication.

## Verification

```sh
python3 -m unittest tests.test_agent_repository_setup -v
```

The integration fixture runs the real settings/requirements/activation services
with separate mock operator GitHub and Forge App clients. It verifies that a
previously undiscovered repository reaches activation, repeat setup avoids
additional administrative writes, and synthetic operator secrets stay out of
Forge calls, persisted server files and result objects. It also exercises plan
editing, changed identities, expired plans, capability restrictions, revocation,
stronger rules, collisions, uncertain writes and subprocess failures.
