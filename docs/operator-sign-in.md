# Operator sign-in

Operator sign-in is the authorization prerequisite for external agent connections
(#15, #122). It protects dashboard data and actions; external CLI connection
approval and scoped client tokens are described in [agent connections](agent-connections.md).

## Configure an existing server once

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

   The managed UI receives only `NITRO_FORGE_PUBLIC_ORIGIN` and
   `NITRO_ALLOWED_HOSTS`, derived from that origin. The secret and session store
   remain on the controller; they are not mounted into the UI container. For a
   manually managed UI, set those two variables yourself to the HTTPS origin and
   its host (including a nondefault port, if used). The HTTPS proxy must preserve
   that Host. Arbitrary `X-Forwarded-Proto` headers do not enable HTTPS actions.
5. Visit the HTTPS address and choose **Continue with GitHub**. Only an account
   whose numeric ID is listed can enter. The header shows its login and **Sign
   out**. Confirm that a private browser window cannot read `/api/settings` or
   open the activity page without signing in.

This is once per Forge server, not per repository. Routine sign-in and sign-out
happen in the UI. Fresh-server installation automation is tracked in #136.

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
boundary. Back up `state/operator-auth` as private controller state. Missing or
invalid initialized state fails closed; do not delete this directory to recover
access. Reconfiguration deliberately replaces it.

Until first configuration, existing deployments retain their previous private
network access model. Once configured, missing/corrupt state does not silently
restore that mode. Do not expose an unconfigured instance publicly.

## Request attribution

Authenticated mutations write an admission record to
`state/operator-auth/requests.jsonl` before service dispatch: operator ID/login,
route and timestamp. Failure to write prevents the mutation. Request bodies,
cookies, OAuth codes, tokens and secrets are excluded. These records mean the
request was admitted, not that its operation succeeded. Outcome correlation,
external retention and the broader audit log are still tracked in #77.

## Credential boundary

GitHub operator identity and future Forge client credentials authorize actions
inside Forge. The external agent's local GitHub administrative identity stays in
its local CLI credential store. This change adds no endpoint accepting that
identity or its token. Worker role Apps and coding provider logins are unchanged.

Protocol reference: [GitHub OAuth web application flow](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps#web-application-flow).
