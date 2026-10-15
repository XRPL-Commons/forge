# Install Forge from your computer

The supported complete flow is `forge install`. It saves an interview, checks the
host and repositories, registers one Forge GitHub App, connects a coding account,
and runs a smoke issue through Triage, Build, Review, Audit and Merge. Only a
GitHub-confirmed merge allows the final repository handover.

## Prerequisites

- A dedicated Ubuntu 24.04 VM with AppArmor, systemd and `/usr/bin/python3`.
- SSH access from your computer as root or an account with noninteractive sudo.
  Configure its SSH key through your VM provider. Forge handles remote commands
  and file transfers through the CLI.
- Local Python 3.10+, Git, OpenSSH, authenticated GitHub CLI (`gh`) and an unlocked
  macOS Keychain or Linux Secret Service.
- Your computer joined to the intended Tailscale network. Enable MagicDNS and
  HTTPS certificates in that network.
- Authority to register a GitHub App under the chosen owner and administer the
  selected repositories. Private repositories need a GitHub plan supporting their
  required branch rules. Forge reports unsupported rules during preflight.

## Install the CLI and start

```sh
uv tool install 'git+https://github.com/XRPL-Commons/forge.git'
forge install --ssh anvil
```

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) first if needed.
If your shell cannot find `forge`, run `uv tool update-shell` and open a new terminal.
`anvil` is an example of an SSH alias; a `user@hostname` destination also works.

The interview records the factory name, GitHub owner and operators, repositories,
merge policies, provider logins and smoke target. It resolves the Forge revision
once. All selected repositories must belong to the chosen GitHub owner. A new
SSH fingerprint must be verified against the VM provider before use.
Review the preflight findings and confirm Apply once.

The default smoke target creates two private repositories under the selected
owner: one for the smoke issue, one for signed-publication qualification. Their
names and ownership markers are bound to this installation. They remain available
for diagnosis after handover, with automation disabled. Choosing a real repository
instead creates a small documentation smoke issue there; its merge policy and
any independent operator holds still apply.

### Browser steps

The CLI prints the exact browser actions as they become available:

1. Approve the VM joining Tailscale if it needs authorization.
2. Open the local GitHub handoff, create the single Forge App, and install it on
   the selected repositories, including the disposable repositories.
3. Sign in to the selected coding provider. Codex uses a URL and one-time device
   code; its login continues on the host if the laptop disconnects.

Keep the CLI running while using its local GitHub callback page. GitHub may ask
for a passkey. If GitHub explicitly reports a missing manifest and no App was
created, return to the same local handoff and use its confirmation to resend that
same manifest. Do not start another installation. An unknown App conversion
outcome remains pending for reconciliation.

Manifest conversion happens locally. Only the App's own credentials are sent to
Forge through SSH stdin. Personal GitHub administration credentials remain local;
no secret belongs in an intent file, command argument or chat message. The same
App provides GitHub sign-in fallback. The installing operator's observed Tailscale
login is linked to their immutable GitHub ID, then verified through HTTPS.

## Resume and inspect

```sh
forge install --ssh anvil --status
forge install --ssh anvil --resume --wait 3600
```

`--status` reads the saved local progress. Apply and resume re-observe incomplete
stages. Host builds and Codex device login survive loss of the client; `--wait`
follows automated progress for a bounded time. A browser action or blocker is
returned explicitly. Resume keeps the original installation and first-merge timer.

If a Codex device login expires, explicitly start a replacement:

```sh
forge account login codex --ssh anvil --retry --json
forge install --ssh anvil --resume --wait 3600
```

For other providers, the existing `forge account login PROVIDER --ssh HOST` guided
login remains available. API keys and long-lived tokens use a hidden prompt or
`--secret-stdin`; the intent contains only the selected provider and login kind.

## Deterministic agent operations

Use the same flow with a non-secret JSON intent:

```sh
forge install --ssh anvil --intent-file install.json --preflight --json
forge install --ssh anvil --apply --wait 3600 --json
forge install --ssh anvil --resume --wait 3600 --json
forge install --ssh anvil --status --json
forge commands --json
```

JSON mode never opens a browser or prompts. Browser actions and progress events
are JSON records on stderr; the final structured result is on stdout. The local
GitHub receiver stays available during its bounded consent wait. The agent can
present its URL while the command is running. Only findings explicitly marked
waivable accept `--waive FINDING_ID`.

Example intent:

```json
{
  "version": 1,
  "ssh": "anvil",
  "name": "Anvil",
  "revision": null,
  "host_key": null,
  "hostname": "anvil",
  "owner": "YOUR_GITHUB_LOGIN",
  "operators": ["YOUR_GITHUB_LOGIN"],
  "repositories": [
    {"name": "OWNER/REPO", "merge_policy": "human", "allow_workflow_edits": false}
  ],
  "providers": [{"id": "codex", "kind": "browser"}],
  "rules_repair": "apply-if-admin",
  "smoke_target": "scratch",
  "codex_parallel_auth": true
}
```

`revision: null` resolves main once. `host_key: null` uses existing SSH trust.
An optional positive `smoke_issue` selects an existing issue in a selected real
smoke repository; otherwise Forge creates its own smoke issue. Workflow-edit
permission is enforced per repository even when another selected repository
requires the App's workflow permission.

## What completion proves

`installed` means the host is ready. `installation_complete` requires all of:

- Current App access and branch-rule checks, including preservation of signing
  requirements and stronger existing protections.
- A qualified database worker image when repository scans require database services.
- A signed-publication probe under the selected App and current target rules.
- Only the smoke issue admitted for execution while installation is pending.
- Real Triage, Build, Review and Audit evidence, owned-worker cleanup, a confirmed
  merge effect, and an independent GitHub read of the exact PR head, merge and
  closed issue.
- An atomic handover applying selected merge/workflow policies and enabling the
  selected repositories. Independent human approval holds remain authoritative.

The local report includes stage receipts and times, installation/source identities,
App and repository results, the smoke receipt and time to first merge. The CLI
prints its location and the Forge URL. A passing synthetic test suite does not
count as this live acceptance. Touch counts and elapsed times must be measured in
an actual run.

## Clean reinstall on a test VM

A reset is a separate, explicit CLI operation. Read its plan before applying:

```sh
forge install --ssh anvil --reset --json
forge install --ssh anvil --reset --apply --reset-revision PLAN_DIGEST --json
```

Repeat the same apply command while `reset_pending` is returned. It fences and
drains owned work and waits for uncertain GitHub effects. It then stops verified
Forge services, removes the dedicated service account and archives its source,
runtime, history and remaining credentials in root-only directories. It archives
matching local setup and consent journals after remote completion is confirmed.

SSH, Tailscale, Ubuntu packages, Docker images and GitHub objects remain. This is
a clean **Forge reinstall on the existing OS**. Provisioning or rebuilding the VM
itself remains a provider operation. After `reset_complete`, run `forge install`
again. Old GitHub App registrations remain visible and can be retired separately.

## Existing installations and individual stages

Use [the upgrade guide](store-migration.md) for an installed factory. Existing
`forge setup plan/apply/status/resume`, repository, account and qualification
commands remain available as deterministic building blocks. An interactive
`forge setup new` without explicit stage options enters the complete interview.
The older identity/role-App commands remain for existing installations; new
installations use the single-App flow above.
