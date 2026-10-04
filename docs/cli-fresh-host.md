# Fresh-host installation from your computer

This is the first implementation increment of
[#136](https://github.com/XRPL-Commons/forge/issues/136). It installs and qualifies
the host, joins its private network, and configures operator sign-in and HTTPS.
GitHub role Apps, coding accounts, activation, and first-merge integration are
still in development.
`installed` means host installation succeeded; it does not mean a factory is
ready to process issues. See the [full acceptance specification](superpowers/specs/2026-10-02-fresh-factory-cli.md).

## Install the client

The client needs Python 3.10+, Git, and OpenSSH on macOS or Linux. Python must
include `venv` support. From a clean clone of this version:

```sh
git clone https://github.com/XRPL-Commons/forge.git
cd forge
sh install-cli.sh --local
forge setup new
```

The repository is currently private, so cloning requires your local GitHub
access. An anonymous curl installer requires a separately published installer
and client distribution; changing the server repository's visibility is not
part of setup. Do not put a GitHub token into a curl command or shell history.

The script installs only the local client into an isolated Python environment
under `~/.local/share/forge-cli`. It links `~/.local/bin/forge`, reports how to use
it if that directory is absent from PATH, and records installation timing in
private local state. It does not change your shell profile or use sudo. An
existing command managed by another installer is preserved. Installation uses
a resolved source commit and checks the new executable before switching the link.

The existing `uv tool install .` method also works from a clone. Its installation
time is not automatically recorded by Forge's installer.

## Guided installation

```sh
forge setup new
```

The wizard asks for the SSH account and address, factory name, host trust, and
installation confirmation. Use a dedicated Ubuntu 24.04 VM with AppArmor enabled
and systemd. The SSH account must be root or have noninteractive sudo; configure
the VM to accept your local SSH key through your VM provider. The remote image
must include `/usr/bin/python3`. No remote terminal or manual file transfer is
needed for the installation steps this command supports.

For a new host, compare the displayed ED25519 fingerprint with the VM provider
console or another trusted source before accepting it. The client pins that key
for subsequent requests. If SSH already knows the host, the existing trust
configuration can be used. Forwarding is disabled in both cases.

The plan shows the host, source commit, and effects:

- Install Ubuntu's Docker Engine, Buildx and Compose plugins, Git, Python, and AppArmor
  tools using the host's package repositories.
- Create the dedicated non-root `forge` account, Docker group membership, and
  lingering user service manager.
- Fetch the chosen Forge commit locally, transfer its commit and tree as a Git
  object pack, and check it out at `/home/forge/forge`. Local Git configuration,
  credentials, and older source history are excluded. The VM does not need a
  GitHub administrator token to obtain the private source.
- Install the matching runner AppArmor policy.
- Invoke the existing `bin/forge setup install`, which builds the UI and worker
  image, runs runner qualification, and starts loopback services.

The runtime is `/home/forge/.local/share/forge`. Host installation does not start
issue processing. An existing `forge` account or source without matching installer
state is a conflict; this command does not adopt it or wipe the VM.

After host installation, the wizard continues into private network access and
operator sign-in. Closing the wizard preserves progress; run `forge setup new
--ssh USER@HOST` to continue.

## Private network and operator sign-in

For a human operator, continue sign-in with:

```sh
forge setup sign-in --ssh USER@HOST
```

The wizard explains which steps happen in GitHub and which the CLI performs.
It opens the registration page, prints the exact App name and callback fields,
asks for the public client ID, and explains how to generate and copy the client
secret before showing a hidden prompt. It also explains passkey verification,
which credential to use, where the secret is stored, and how to resume. You do
not need an agent to fill the GitHub form. GitHub registration and passkey checks
remain human browser steps; the CLI does not create the App automatically.

The command uses your saved factory settings, so it skips registration when the
client ID is already known. Terminal output is readable text. `--json` retains
the noninteractive protocol, and `identity` remains an alias-compatible command.

These stages also have noninteractive commands:

```sh
forge setup network --ssh USER@HOST --json
forge setup network --ssh USER@HOST --apply --wait 60 --json
forge setup identity --ssh USER@HOST --operator GITHUB_LOGIN --json
forge setup identity --ssh USER@HOST --client-id PUBLIC_CLIENT_ID --json
forge setup identity --ssh USER@HOST --apply
```

Network setup installs Tailscale from its signed Ubuntu repository and returns
a browser link for joining your tailnet. Approve the VM, then run `forge setup
network --ssh USER@HOST --json` again. `network_ready` confirms its private HTTPS
name; the UI still listens on loopback. Hostname defaults to the factory name
and is retained across retries. Use `--hostname` on the first network operation
to choose another name.

Identity setup resolves operator GitHub logins locally and returns the exact
registration fields for a dedicated GitHub OAuth App. Create the App through
the returned browser link, then supply its public client ID. The client remembers
the operator, client ID, and verified network origin. It prompts privately for
the identity App secret when applying. Agents can instead use `--apply
--secret-stdin --json` with a private secret source; never put the secret in shell
arguments, agent messages, or a setup journal.

The CLI invokes the existing sign-in bootstrap as the installed `forge` account.
It configures the authorized operator IDs, reconciles loopback services, enables
private Tailscale HTTPS, and verifies HTTPS from both computers. The secret is
sent only through encrypted SSH stdin and stored in Forge's private runtime.
Local GitHub administrative credentials are not transferred.

| Status | Meaning and next action |
| --- | --- |
| `network_required` | Review and apply private network setup. |
| `network_installing` | The durable network job is running; poll the same command. |
| `tailnet_authorization_required` | Open the returned link, approve the VM, then poll. |
| `tailnet_approval_required` | Approve the VM in the returned Tailscale admin page, then poll. |
| `network_interrupted` | Correct the reported prerequisite and retry with `--apply`. |
| `network_ready` | The VM has a private network address; continue to identity setup. |
| `identity_app_required` | Register the identity App using the returned fields. |
| `identity_secret_required` | Supply the App secret through the private input method. |
| `planned` | Review the identity plan and apply it. |
| `sign_in_required` | HTTPS is verified; open the returned login page and sign in. |

After an interrupted identity apply, use `forge setup identity --ssh USER@HOST
--resume --json`. Existing identity settings, sessions, and grants are preserved.
For humans, `forge setup sign-in --ssh USER@HOST --resume` prints the result and
next step in plain language. If settings were saved before an HTTPS check failed,
the CLI says that the secret is saved and gives this exact recovery command.
Different operator or App settings produce a conflict rather than replacing
them. A stopped network installer can be repaired explicitly with `forge setup
network --ssh USER@HOST --apply --upgrade-installer`; the source commit and setup
identity remain pinned.

`sign_in_required` confirms configuration, not a completed browser sign-in or a
working factory. Role Apps, coding accounts, repository readiness, explicit
activation, and an issue merged through Forge remain acceptance requirements.

## Noninteractive operations

Discover command schemas with `forge --json commands`. JSON mode never prompts
or opens a browser. Host-key discovery does not trust the returned key:

```sh
forge setup host-key --ssh root@HOST --json
forge setup new --ssh root@HOST --name Anvil --host-key SHA256:VERIFIED_FINGERPRINT --json
forge setup apply --ssh root@HOST --json
forge setup status --ssh root@HOST --json
forge setup resume --ssh root@HOST --wait 60 --json
```

`setup new --json` and `setup plan` create a plan. Apply requires a saved plan.
Use `--revision FULL_COMMIT_SHA` on the plan to select an exact source version;
otherwise the client resolves main once and saves that commit. Resume keeps it.

| Status | Meaning and next action |
| --- | --- |
| `planned` | The VM passed initial inspection; review the plan and apply it. |
| `installing` | A systemd job owns installation; poll status or resume watching. |
| `interrupted` | Installation stopped; inspect the named stage and use resume to retry. |
| `installed` | Host qualification and initial service checks passed; full factory onboarding remains required. |

Results include the setup ID, destination, pinned revision, host capacity, current
step, completed steps, error code, and next action. Raw subprocess diagnostics
and credential values are not returned. Host packages use the configured Ubuntu
package repositories; pinning Forge's commit is not a claim that every OS package
or network response is reproducible byte for byte.

## Interruptions and clean-install tests

The remote systemd job survives a closed terminal or lost SSH connection. Local
state is mode 0600 under `~/.local/state/forge-client`. Installer state and logs
are private under `/var/lib/forge-install` on the VM. Retrying reuses the setup
identity, skips completed stages, and reruns an interrupted stage idempotently.
Status does not resume or start an installation. The original client installer
payload is pinned too; use that client version to resume an interrupted run.

When an installer defect is fixed, `forge setup resume --ssh USER@HOST
--upgrade-installer` explicitly replaces the stopped installer implementation.
It preserves the setup ID, pinned server source, start time and completed
stages. Changed package prerequisites are reconciled again. It cannot replace a running job and does not upgrade the Forge server
source. Ordinary resume never changes the installer implementation silently.

After deliberately rebuilding the disposable VM, verify its new host identity
and start a new measurement run:

```sh
forge setup new --ssh root@HOST --name Anvil --new-run
```

`--new-run` archives the previous local run only after confirming that the target
is fresh. It never wipes a host or overrides an existing remote installation.
The existing Forge production server is outside this test workflow.

The current `setup_elapsed_seconds` is a stage measurement. It is not the primary
time-to-first-merge metric: that requires the CLI-installation start and evidence
of an issue completed and merged through Forge's normal checks and reviews.
No remote telemetry is sent.
