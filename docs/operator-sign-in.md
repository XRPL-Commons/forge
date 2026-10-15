# Operator sign-in

Operator sign-in is the authorization prerequisite for external agent connections
(#15, #122). It protects dashboard data and actions; external CLI connection
approval and scoped client tokens are described in [agent connections](agent-connections.md).

## Configure an existing server once

For a private Tailscale host, use the local
[`forge server setup` command](cli-server-setup.md). It resolves operator IDs,
transfers the identity secret privately over SSH, configures services and HTTPS,
and supports retries without revoking sessions.

This is once per Forge server, not per repository. Routine sign-in and sign-out
happen in the UI. The [fresh-host wizard](cli-fresh-host.md) includes this stage
and then guides role App registration under the configured operator session.
`forge server setup` continues to require an existing host.

## Sessions and recovery

Sessions expire after eight hours and use Secure, HttpOnly, SameSite=Lax,
`__Host-` cookies. Sign-in state expires after ten minutes, is bound to the browser,
and is consumed before code exchange. PKCE binds that exchange to the sign-in
attempt. GitHub identity tokens are used transiently for `/user`; Forge stores
only the resulting ID/login, a session-token hash, expiry and CSRF value.

`forge server setup` configures sign-in once and refuses to replace a different
existing configuration; there is no command to replace the operator list or the
identity App (#259 moves operator identity to Tailscale). Explicit sign-out also revokes client connections
approved by that operator; automatic browser-session expiry does not. The host account remains the administrative recovery
boundary. On the selected SQLite runtime, identity, sessions, grants and audit
history live in the private database. The identity client secret is a private,
digest-checked file under its `credentials/` directory. Follow the
[store backup procedure](factory-runtime-reference.md); copying an active WAL database as
one ordinary file is not a consistent backup. Missing or invalid initialized
state fails closed. Reconfigure through the CLI to recover access; do not delete
the database. Before SQLite selection, the legacy private `state/operator-auth`
directory remains the identity authority.

Until first configuration, existing deployments retain their previous private
network access model. Once configured, missing/corrupt state does not silently
restore that mode. Do not expose an unconfigured instance publicly.

## Request attribution

On the selected SQLite runtime, the API records mutation requests in append-only
`operator_audit` rows. A browser request has a durable `request_started` claim
before dispatch and one terminal `request_outcome`: **accepted**, **rejected**, or
**unknown** when interruption prevents a reliable result. A safely identified
operator's validation or CSRF rejection is attributed to the verified operator
and existing session row. Failed sign-in, invalid upstream requests and other
unverified requests do not acquire an operator identity. Requests refused by
Nitro or before the selected database can be opened are outside this audit.

Receipts contain the route, time, verified operator/session or client connection,
request identity, a validated target and expected revision where applicable,
and a coded reason. They exclude request/response bodies, free-form reasons,
cookies, OAuth codes, CSRF values, bearer tokens and exception text. Session
references are database row IDs, never raw session cookies.

Callers may send a UUID or 32-character lowercase hexadecimal
`X-Forge-Request-ID` and retain it for a retry. Existing body `request_id` fields
(and client operation `id`) take precedence. The response carries the caller
identity in the same header. Forge scopes it to the verified principal, client
connection and route; changing the body does not grant permission to reuse an
identity for another mutation. A repeated browser request receives HTTP 409 and
the prior receipt instead of rerunning the handler. A concurrent request can
report `in_progress`; an unfinished claim after interruption becomes `unknown`.
Reload the current state and reconcile any linked command before submitting a
new identity. A receipt's uniqueness does not promise exactly-once GitHub effects.

Client repository operations keep their existing durable operation ID and
immutable request binding. Repeating that operation returns its stored status;
its accepted request receipt is distinct from its eventual completion/failure
record. Lifecycle commands, independently reconciled GitHub effects and
configuration revisions retain their own durable identities. Audit command
links connect them to the initiating request. Configuration changes record only
before/after digests, domain and revision in the same transaction as the change;
background client operations retain the approved client attribution. Direct
local CLI/host configuration changes carry the actor `local:host` and have no
HTTP request receipt.

Reset preserves this history. One-way migration preserves legacy admission
records as `admitted`; it does not invent outcomes for them. Before SQLite
selection, legacy requests continue using the private JSONL admission log. No
new operational JSONL audit writer is used after selection.

## Credential boundary

GitHub operator identity and approved Forge client credentials authorize actions
inside Forge. The external agent's local GitHub administrative identity stays in
its local CLI credential store. This change adds no endpoint accepting that
identity or its token. Worker role Apps and coding provider logins are unchanged.

Protocol reference: [GitHub OAuth web application flow](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps#web-application-flow).

## Migrating an existing installation to the Forge App

After upgrading the installed release, use the CLI from your computer:

```sh
forge server identity plan --ssh HOST --source /absolute/forge/source --root /absolute/forge/runtime --service-user forge --json
```

The plan reports the installed App, permission and repository coverage blockers,
and a configuration revision. Accept any required permission changes on GitHub
before applying the App step with the returned revision:

```sh
forge server identity apps --ssh HOST --source /absolute/forge/source --root /absolute/forge/runtime --service-user forge --revision REVISION --json
```

This preserves retired App identities and runs a fresh repository protection
check. If the factory is paused or another refresh is running, the response shows
`protection_check: pending`; repeat the step once that condition clears. A
completed check can still report repository rules that need setup. Configure the
Forge App's sign-in callback, then run the `sign-in` step
with the same host options. It prompts privately for that App's client secret;
automation can use `--secret-stdin`. Secrets never belong in command arguments.
The `tailscale` step selects trusted Tailscale identity; `--mode github` selects
GitHub sign-in. Existing linked operators and trusted Serve ingress are required
for Tailscale access. Signing out revokes approved agent connections and removes
the automatic Tailscale link; an explicit GitHub sign-in restores it. The plan is read-only; each named mutation can be resumed
by repeating it. These commands do not edit GitHub permissions or uninstall old
Apps. Keep the old Apps until pending publications have been reconciled.
