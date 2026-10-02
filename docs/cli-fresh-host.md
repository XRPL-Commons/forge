# Fresh-host installation from your computer

This is the first implementation increment of
[#136](https://github.com/XRPL-Commons/forge/issues/136). It installs and qualifies
the host. The complete wizard through operator sign-in, private HTTPS, GitHub
Apps, coding accounts, activation, and first merge is still in development.
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

Once this installer is published on main, the equivalent one-line installation
will be:

```sh
curl -fsSL https://raw.githubusercontent.com/XRPL-Commons/forge/main/install-cli.sh | sh
```

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

- Install Ubuntu's Docker Engine and Compose plugin, Git, Python, and AppArmor
  tools using the host's package repositories.
- Create the dedicated non-root `forge` account, Docker group membership, and
  lingering user service manager.
- Check out the chosen Forge commit at `/home/forge/forge`.
- Install the matching runner AppArmor policy.
- Invoke the existing `bin/forge setup install`, which builds the UI and worker
  image, runs runner qualification, and starts loopback services.

The runtime is `/home/forge/.local/share/forge`. Host installation does not start
issue processing. An existing `forge` account or source without matching installer
state is a conflict; this command does not adopt it or wipe the VM.

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
