# Connect an external agent

The local client connects once, prepares repositories with local GitHub authority,
and asks Forge to verify and enable them through the same services used by the UI.
Connections have explicit scope, short-lived credentials and revocation.
Fresh Forge installation stays in #136.

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
Forge's Apps therefore needs an explicit `--repo OWNER/REPO` connection selection
or its App access established locally first. Explicit repository selections let
the CLI prepare a repository even when no Forge App can initially see it.

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

`/api/client/{device,token,refresh,status,disconnect,requirements,authorize,read,operation}` are closed JSON
POST endpoints. Device polling uses the pending, slow-down, denial and expiry
semantics of [RFC 8628](https://www.rfc-editor.org/rfc/rfc8628); this is a Forge JSON
protocol, not a general-purpose OAuth server. Cookies and browser Origin headers
are rejected on these routes, while operator approval routes require the normal
signed-in session and CSRF checks. Authorization headers are forwarded only for
the explicit client routes. All transport uses verified HTTPS with redirects
rejected; remote responses cannot choose another credential destination.

Prepare/activate capabilities authorize verification and activation operations.
Each endpoint checks the grant, target and reset generation itself. Configuration
writes also check the current revision and fresh readiness. These capabilities
do not unlock browser action routes.

## Repository operations

```sh
forge repo inspect OWNER/REPO --server https://forge.example --json
forge repo plan OWNER/REPO --server https://forge.example --json
forge repo setup OWNER/REPO --server https://forge.example --apply --enable --json
```

`--apply` allows local App repository grants and the additive Forge review
baseline. `--enable` explicitly allows starting automation after verification;
without it setup stops at readiness. Setup without `--apply` only plans.
The local broker uses fixed GitHub operations and preserves existing grants,
unrelated rules and stronger policy. App identities are pinned on the connection's
first requirements read and checked on subsequent repositories and token refreshes.

GitHub's [installation grant endpoint](https://docs.github.com/en/rest/apps/installations#add-a-repository-to-an-app-installation)
requires repository administration. GitHub currently documents fine-grained
tokens with the “GitHub App installation repository access” write permission
for this operation. The client uses the existing local credential and never
expands it automatically. If GitHub refuses the grant, the client
returns an installation settings URL and `needs_user_action`; grant access in the
browser and resume. No administrative token is sent to Forge.

```sh
forge repo setup OWNER/REPO --server https://forge.example --apply --enable --resume --json
```

Resume uses the same activation intent and pinned identity. A private, secret-free
journal under `~/.local/state/forge-client` records workflow IDs, local write
progress and outstanding server operations. Server records under
`state/client-operations` bind operation IDs to the approved client, operator,
target, action, revision, instance and generation. They contain no credentials.
Polling an ID after a server restart resumes verification. Duplicate IDs cannot
change their binding, and interrupted activation reconciles the exact expected
configuration revision. Concurrent changes return a conflict.

`--wait` bounds each operation's wait (default 120 seconds, maximum 600); `--wait 0`
returns immediately. A pending result includes an operation ID and can be resumed.
An uncertain ruleset create is independently verified before proceeding. If it
cannot be verified, `write_unconfirmed` links to GitHub for inspection; the CLI
does not create a duplicate. Do not delete the journal to retry uncertain writes.

`ok: true` means a valid result, which can still be pending or blocked. Check
`status`: `ready` and `enabled` are the successful setup outcomes. Errors include
a safe reason; browser handoffs and blockers include next actions. JSON mode
never prompts or opens a browser.

Other scoped commands:

| Command | Result |
| --- | --- |
| `repo verify` | Refresh App access and effective protection |
| `repo enable` / `repo disable` | Explicitly change repository activation |
| `repo activity` | Factory health timing and this repository's issue/job state |
| `repo issues --page N` | Paginated observed issues |
| `repo issue OWNER/REPO NUMBER` | Observed issue detail and decisions |

All take `OWNER/REPO`, `--server HTTPS_ORIGIN` and `--json`. Reads reuse UI services
and expose observation freshness. Verification refreshes observations; cached
issue reads do not launch work. Results omit other repositories and raw worker
logs. The API does not expose host administration or generic worker controls.

## Shared agent skill

The package includes [forge-repositories/SKILL.md](../forge_client/skills/forge-repositories/SKILL.md).
Use `forge skill` to print it or `forge --json skill` for structured output, then
install that file in your agent's skill directory. It covers connection approval,
setup intent, JSON statuses, observation and resumption.
