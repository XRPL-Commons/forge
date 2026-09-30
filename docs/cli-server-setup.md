# Set up operator sign-in with the local CLI

`forge server setup` configures sign-in and private HTTPS on an **existing Forge
host**. Run it once per server. After setup, operators sign in through the UI;
adding repositories does not require repeating this command.

The local CLI uses your SSH authority for host configuration and your local
GitHub CLI to resolve operator usernames to numeric IDs. GitHub administrative
credentials stay on your computer. Only the dedicated identity OAuth App's
client secret is transferred to Forge, over SSH stdin. It is stored in private
controller state, outside workers and the UI container.

## Prerequisites

- Install the [standalone client](agent-connections.md#install-on-the-agents-computer)
  from this version of the repository. The host checkout must also contain this
  version; setup does not fetch or switch source revisions.
- The selected Linux host already has Forge installed with `bin/forge setup`,
  a private runtime directory owned by its service account, and running Tailscale.
- Configure local SSH access to that account and verify the host key. Setup uses
  batch SSH authentication, requires a known host key, and disables forwarding.
- That account must be able to reconcile its existing Forge services and run
  `sudo -n tailscale serve`. Tailscale HTTPS must be available for its own
  `*.ts.net` name. This command supports private Serve on port 443.
- Sign in to `gh` locally. Operator names are resolved using GitHub's user API.

Fresh installation, role App registration and provider setup remain in #136.
For an existing custom reverse proxy, use the
[host configuration instructions](operator-sign-in.md#manual-host-configuration).

## Inspect the plan

Use explicit paths on the selected SSH host:

```sh
forge --json server setup \
  --ssh forge \
  --source /home/luc/forger-remote \
  --root /home/luc/ops/forge-watchdog \
  --origin https://forge.tail7560bd.ts.net \
  --operator lucbocahut
```

Without a client ID, an unconfigured server returns `identity_app_required`, a
GitHub registration link, and the exact homepage and callback fields. Create or
reuse a **dedicated identity-only GitHub OAuth App** with those fields. Complete
GitHub's registration, consent and passkey checks in your browser. This App is
separate from Forge's four role Apps and requests no OAuth scopes.

Repeat the command with `--client-id YOUR_IDENTITY_APP_CLIENT_ID` to inspect the
setup steps. Repeat `--operator` for additional operator accounts. Planning
checks the host but does not configure identity, restart services or change HTTPS.
The plan can create private state directories and lock files.

## Apply once

Repeat the same command with `--client-id` and `--apply`, omitting `--json`.
The CLI asks for the App's client secret at a hidden terminal prompt. Do not put
it in a command argument, chat message or shell history. A GitHub personal access
token is not an identity App client secret.

External agents can use `--json --apply --secret-stdin` with secret input supplied
through a private pipe by the operator or their credential manager. JSON mode
never prompts or opens a browser. Without private input, it returns
`identity_secret_required` so the agent can hand that step back to the operator.
The CLI does not persist the bootstrap secret locally or print it in results.

Apply configures identity, reconciles services from the selected checkout with
the UI bound to `127.0.0.1:8787`, and enables private Tailscale HTTPS forwarding
to that UI. It verifies the sign-in endpoint from both the host and your computer.
Normal host-update qualification and checks still apply. Existing HTTP access
is replaced by the HTTPS address; an interrupted apply may leave UI access
unavailable until resumed.

The result `sign_in_required` includes the HTTPS login link. Open it and choose
**Continue with GitHub** to verify your operator account. Then run
[`forge connect`](agent-connections.md#connect-once) to request a scoped agent
connection. HTTPS verification alone does not prove the OAuth App secret works;
the first completed browser sign-in does.

## Resume and recovery

If SSH disconnects, the updater fails, or DNS/TLS is not ready, repair the reported
host prerequisite and repeat the command with `--resume`. Once the secret has
been saved, resume does not need it again. If no secret was saved, use `--apply`
with private input. The client ID can be omitted when the host already has it.

Reruns preserve existing sessions, pending sign-in flows and client connections.
Setup refuses different existing identity settings, another application's Serve
configuration, or public Funnel configuration. It serializes concurrent
bootstraps and rejects changed plans or reset generations. It never automatically
replaces a conflicting identity or proxy configuration.

To intentionally replace a client secret or change authorized operators, use
the [host recovery command](operator-sign-in.md#sessions-and-recovery). That
separate operation revokes sessions. There is no web endpoint accepting bootstrap
secrets and no anonymous first-visitor claim.
