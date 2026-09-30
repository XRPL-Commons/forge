# Connect an external agent

This implements the connection step of #122: browser approval, repository/owner
scope, short-lived credentials, revocation, and a local client. The client can
connect, inspect its grant, disconnect and read repository requirements.
Repository apply/verify/enable commands, durable operations and the shared agent
skill remain the next step. Fresh Forge installation stays in #136.

## Prerequisites

Use an existing, active Forge server with [operator sign-in](operator-sign-in.md)
and verified HTTPS configured. The Agent connections settings page shows the
missing prerequisite on older private-network deployments. Client endpoints
remain disabled there; browser action tokens cannot substitute for sign-in.
The local [`forge server setup`](cli-server-setup.md) command can configure that
prerequisite on an existing Tailscale host over SSH.

The local client currently supports macOS and Linux with Python 3.10 or newer,
`gh`, and an unlocked macOS Keychain or Linux Secret Service. GitHub authentication
stays in the local `gh` credential broker. The client never requests token output.

## Install on the agent's computer

From a checkout of this repository, install the isolated client package:

```sh
pipx install .
forge --json commands
```

The package contains `forge_client` only, not the server. Its installed `forge`
command is separate from the host administration entry point `bin/forge`.
No PyPI release has been published. A virtual environment and `pip install .`
are an alternative to pipx. No user credentials are included in the package.

## Connect once

```sh
forge connect https://forge.example --name 'My local agent' --owner XRPL-Commons
```

The client resolves the GitHub owner to a numeric ID using local `gh`, shows a
short code and opens **Settings → Agent connections**. Sign in if needed. Click
**Review request**, compare the code with your client, choose the allowed actions
and repository scope, then approve. You can narrow the request before approval.

An owner scope covers that owner's current and future repositories. A selected
repository scope can cover several repositories: repeat `--repo OWNER/REPO`.
`--read-only` requests only repository reads. An owner transfer is checked from
fresh GitHub metadata before each scoped repository read. If Forge cannot verify
that metadata, it refuses the read. An initially private repository invisible to
Forge's Apps therefore needs its App access established locally first; this
connection release does not yet automate that preparation workflow.

The client name and requested repository labels come from the requesting client.
Check the displayed IDs and scope. Forge verifies owner IDs independently at
approval and again checks repository ownership when using an owner-scoped grant.
Repository selections bind both the immutable ID and the reviewed full name;
a rename or transfer requires a new reviewed connection for that selection.

For an external agent that should return instructions without opening a browser:

```sh
forge --json connect https://forge.example --name 'My agent' --owner XRPL-Commons
# Relay the returned code/link to the operator, then resume the same request:
forge --json connect https://forge.example --resume --wait 600
forge --json auth status https://forge.example
forge --json requirements https://forge.example XRPL-Commons/example
forge --json disconnect https://forge.example
```

JSON mode never opens a browser or prompts. A pending request is kept in the OS
keyring so another invocation can resume it. The private polling credential is
never returned in output. With no `--wait`, resume polls only when due and returns
pending immediately. There is one saved connection per canonical server origin.
Use **Revoke access** in the UI to stop access from a lost client.

## Lifetime and revocation

- Connection requests expire after ten minutes; approval requires an authenticated
  operator, a matching code and an explicit confirmation. Polling starts at five
  seconds and slows down after early polls. A code can be redeemed once.
- Access credentials expire after 15 minutes. Refresh credentials rotate, expire
  after 30 idle days, and cannot extend the connection past 90 days.
- Replaying a used refresh credential revokes that connection's entire token
  family. A lost rotation response may require reconnecting. Local commands use
  a process lock to prevent accidental concurrent rotations.
- Revocation, explicit operator sign-out, operator reconfiguration/removal, and a
  factory reset invalidate affected credentials. Browser session expiry alone
  does not disconnect an otherwise authorized client.
- The server persists token hashes. The client requires a supported OS keyring;
  it does not fall back to plaintext files. The local lock file contains no
  credential or connection data. Network/keyring errors are sanitized.

The browser lists names, approvers, scopes, creation/expiry, status and last use.
Revocation prevents subsequent requests; it cannot undo a GitHub write already
accepted through a separate local GitHub credential. Grant changes require a new
connection approval. Client grants cannot mint other grants, read secrets, run
host commands, control workers, reset Forge, or use generic operator APIs.

## Protocol and implementation boundary

`/api/client/{device,token,refresh,status,disconnect,requirements}` are closed JSON
POST endpoints. Device polling uses the pending, slow-down, denial and expiry
semantics of [RFC 8628](https://www.rfc-editor.org/rfc/rfc8628); this is a Forge JSON
protocol, not a general-purpose OAuth server. Cookies and browser Origin headers
are rejected on these routes, while operator approval routes require the normal
signed-in session and CSRF checks. Authorization headers are forwarded only for
the explicit client routes. All transport uses verified HTTPS with redirects
rejected; remote responses cannot choose another credential destination.

Prepare/activate capabilities record the approved future workflow scope. This
release exposes repository requirements only; those capabilities do not unlock
any existing browser action route. The write endpoints must independently check
the same grant, target, revision and reset generation when added.
