# Operator sign-in

Operator sign-in is the authorization prerequisite for external agent connections
(#15, #122). It protects dashboard data and actions; external CLI connection
approval and scoped client tokens are described in [agent connections](agent-connections.md).

## Configure an existing server once

For a private Tailscale host, use the local
[`forge server setup` command](cli-server-setup.md). It resolves operator IDs,
transfers the identity secret privately over SSH, configures services and HTTPS,
and supports retries without revoking sessions.

### Manual host configuration

1. Arrange verified HTTPS for the existing Forge UI using your reverse proxy or
   private network HTTPS service. Forward to the UI, never directly to the Python
   status API. Keep the current private network restrictions during setup.
2. Register a **dedicated GitHub OAuth App for operator identity**. Its homepage
   is your canonical HTTPS origin, for example `https://forge.example`. Its
   callback is that origin plus `/auth/github/callback`. This identity app is
   separate from the four role Apps. Forge requests no OAuth scopes and rejects
   a token returned with additional scopes. Do not reuse an administrative OAuth
   App or enter a personal access token here.
3. On the Forge host, configure the identity app and explicit numeric GitHub
   account IDs. Verify each intended operator's ID from their GitHub profile API;
   account names can change. Repeat `--operator-id` for each operator:

   ```sh
   bin/forge operators --root /path/to/forge-runtime \
     --origin https://forge.example \
     --client-id YOUR_IDENTITY_APP_CLIENT_ID --operator-id 12345
   ```

   Enter the identity app's client secret at the hidden prompt. It is never a
   command-line argument. No GitHub administrator token or worker credential is
   needed. Configuration immediately enables API authorization and revokes all
   existing sessions. There is no “first visitor becomes administrator” step.
4. Reconcile the service configuration from the same checkout:

   ```sh
   bin/forge update --root /path/to/forge-runtime --no-pull
   ```

   For identity configuration, the managed UI receives only
   `NITRO_FORGE_PUBLIC_ORIGIN` and `NITRO_ALLOWED_HOSTS`, derived from that origin.
   Its separate upstream secret authenticates the UI proxy to Python. The identity
   App client secret and session store remain on the controller; they are not
   mounted into the UI container. For a
   manually managed UI, set those two variables yourself to the HTTPS origin and
   its host (including a nondefault port, if used). The HTTPS proxy must preserve
   that Host. Arbitrary `X-Forwarded-Proto` headers do not enable HTTPS actions.
5. Visit the HTTPS address and choose **Continue with GitHub**. Only an account
   whose numeric ID is listed can enter. The header's account icon opens a menu
   with the login and **Sign out**. Confirm that a private browser window cannot
   read `/api/settings` or
   open the activity page without signing in.

This is once per Forge server, not per repository. Routine sign-in and sign-out
happen in the UI. Fresh-server installation automation remains unmerged work in
#136; the released
`forge server setup` requires an existing host and does not install a fresh VM.

## Sessions and recovery

Sessions expire after eight hours and use Secure, HttpOnly, SameSite=Lax,
`__Host-` cookies. Sign-in state expires after ten minutes, is bound to the browser,
and is consumed before code exchange. PKCE binds that exchange to the sign-in
attempt. GitHub identity tokens are used transiently for `/user`; Forge stores
only the resulting ID/login, a session-token hash, expiry and CSRF value.

Re-run `bin/forge operators` on the host to replace the operator list or identity
app configuration and revoke every session and pending sign-in. This is also the
recovery path for a lost operator account. Removing an operator takes effect
without restarting Python. Explicit sign-out also revokes client connections
approved by that operator; automatic browser-session expiry does not. The host account remains the administrative recovery
boundary. On the selected SQLite runtime, identity, sessions, grants and audit
history live in the private database. The identity client secret is a private,
digest-checked file under its `credentials/` directory. Follow the
[store backup procedure](store-migration.md); copying an active WAL database as
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
