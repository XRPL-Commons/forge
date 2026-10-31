# Forge

Forge moves GitHub issues through **Build → Review → Security audit → Merge**.
GitHub holds requirements, code and review evidence. The supervisor owns the durable
delivery ledger, assignments, failure history and resource allocation. Related issues
can form one operator-approved delivery with one owning builder, tracked parallel
children and one integrated pull request for Review and Audit. Workers receive
one assignment and run inside disposable containers; completion is verified against
GitHub before the issue advances.

## Install

This repository contains the Forge server, workers, web UI and local CLI. Install
the CLI on your computer or your external agent's computer, then use
`forge install` to install and configure the server on its Linux host.

### Local CLI

The CLI applies schema changes to an installed host with a resumable
`forge migrate upgrade`. See [the upgrade guide](docs/store-migration.md).

On macOS or Linux with Python 3.10+, Git and
[uv](https://docs.astral.sh/uv/getting-started/installation/) installed:

```sh
uv tool install 'git+https://github.com/XRPL-Commons/forge.git'
forge --help
forge install --help
forge server setup --help
```

If your shell cannot find `forge`, run `uv tool update-shell` and open a new terminal.

This fetches the repository but installs only the `forge-agent-client` Python
distribution: the `forge` client command, `forge_client`, and the `forge` Python
modules used for shared contracts and host maintenance. Installing this package
does not build worker images, deploy services or start a server. No PyPI release
is currently published.

| Command | Run it on | Purpose |
| --- | --- | --- |
| `forge install` | Your computer or the external agent's computer | Interview or JSON intent for Ubuntu 24.04, single-App consent, coding login, smoke merge and repository handover |
| `forge migrate` | Your computer or the external agent’s computer | Rehearse a private SQLite import, or activate it through a resumable one-way migration |
| `forge server setup` | Your computer or the external agent's computer | Configure operator sign-in and private HTTPS on an existing Tailscale host over SSH |
| `bin/forge setup` | The Forge server, from its source checkout | Install services, prepare workers and connect coding accounts |
| `bin/forge update` | The Forge server, from its source checkout | Update the source and reconcile the installed services |

For a fresh VM, follow [fresh-host installation](docs/cli-fresh-host.md). Run
`forge install` for the guided path through single-App setup and a first verified
smoke merge. `forge install --ssh USER@HOST --resume --wait 3600` resumes it.
Agents use the same stages with `--intent-file` and `--json`. The CLI handles SSH
and transfers and keeps GitHub administration local. Only the smoke issue runs
before verified handover enables the selected repositories.

For an existing Forge host that needs operator sign-in and private HTTPS, use
[CLI server setup](docs/cli-server-setup.md). Then follow
[agent connections](docs/agent-connections.md) to approve a scoped external agent
connection. Connections require an unlocked macOS Keychain or Linux Secret
Service for local credential storage.

For repository setup on an existing server:

```sh
forge repo setup OWNER/REPO --server https://forge.example --apply --enable --json
```

This uses the approved client connection, keeps GitHub administration local, and
asks Forge to verify readiness before enabling automation. Omit `--enable` to stop
at readiness, or `--apply` to return a plan. See [repository operations](docs/agent-connections.md#repository-operations)
and the bundled [agent skill](forge_client/skills/forge-repositories/SKILL.md).

GitHub branch protection is configurable per repository. For private personal
repositories without GitHub Pro, add `--github-protection optional`, or choose
**Optional** under the repository's **GitHub branch protection** setting in the UI.
Forge still requires its Review, Audit and merge checks; existing GitHub rules
remain in force. Changing this setting through the CLI requires an approved
policy grant. See [optional protection](forge_client/README.md#optional-github-branch-protection).

### Forge server

On an AppArmor-enabled Linux VM with Docker Engine (Compose plugin), systemd and
Python 3.10+, as a dedicated non-root account in the `docker` group with lingering
enabled (an administrator also installs the runner's AppArmor profile unless that
account may use sudo; see [installation](docs/installation.md) for these commands):

```sh
git clone https://github.com/XRPL-Commons/forge.git
cd forge
bin/forge setup      # host, runner image, operator UI, coding-account logins
```

Then open `http://127.0.0.1:8787/onboarding` (through an SSH tunnel, or on your
tailnet address with `--bind`) to connect GitHub, choose a repository and start.
Label an issue `ready` to have it worked. To apply a newer release later:

```sh
bin/forge update
```

See [installation](docs/installation.md), [guided onboarding](docs/onboarding.md),
the [issue state machine and blockers](docs/issue-state-machine.md) and the
[workflow contract](docs/software-factory-spec.md).
The [current architecture target](docs/target-architecture.md) records the agreed
move to one GitHub App, tokenless workers and Tailscale identity, including the
proofs and migration steps still required.

**Status: experimental.** The durable factory has synthetic workflow/restart tests
and real credential-free runner canaries. The guided setup verifies access, provider
authentication and disposable runner cleanup before Start; GitHub App approvals
still happen on GitHub. See [implementation status](docs/implementation.md).

## Plan repository work

Each repository has durable planning rules, notes and proposals. In **Repository
settings → Plan repository work**, request **Triage now**, inspect the proposal,
then approve its rows. Nightly proposals are enabled by default. Planning can
propose closures with evidence, labels, parked issues, prerequisites, questions
and related groups; GitHub changes require operator acceptance.

Every composition is approved explicitly, including groups with existing PRs.
Choose a new PR or one exact existing PR to continue. One logical builder owns
all member requirements, delegates tracked children within shared capacity, then
integrates their outputs. Review and Audit assess the complete candidate; changed
inputs require revalidation. Other existing PRs are preserved.

**Issue Details → Combined delivery** loads member and child progress on demand.
The same read is available to an external agent:

```sh
forge repo delivery OWNER/REPO ISSUE_NUMBER --server https://forge.example --json
```

Merge policy defaults to `human` per repository. An explicit `auto` policy removes
the ordinary counted-approval requirement while keeping CI, Review, Audit,
verification and any additional human requirement. Settings shows this policy
read-only; change it through `forge repo policy` after approving its separate
connection capability.

See [repository planning](docs/repository-planning.md),
[delivery execution](docs/delivery-execution.md), and
[merge policy](docs/factory-runtime.md#what-permits-a-merge).

## Dashboard and history

Guided onboarding creates the GitHub Apps and configuration. The
[GitHub settings guide](docs/github-settings.md) is the reference for the `/settings`
page, repository discovery, explicit activation and the underlying files; secrets
remain outside the source tree. The factory issues repository-scoped role credentials.

The operator UI is the Nuxt app in `ui/` (service `forge-ui`); it reads local
state through the JSON API in `legacy/forge-status.py`, which listens only on
loopback. `bin/forge setup` installs both and `bin/forge update` keeps them current;
see [`docs/ui-deployment.md`](docs/ui-deployment.md). The pages refresh automatically.

[Optional operator sign-in](docs/operator-sign-in.md) protects dashboard data and
actions using an explicit list of authorized GitHub account IDs. It requires
verified HTTPS and is required for external agent connections. Before sign-in is
configured, keep the UI on loopback or a trusted private network; it exposes local
worker output and must not be public. Operator actions use same-origin requests
and CSRF protection; the API queues them and the supervisor executes them.

The local CLI keeps your GitHub administrative credentials on your computer.
Forge stores its own required runtime credentials privately on the controller:
the operator identity App secret, role App credentials and coding-provider logins.
The identity App requests no OAuth scopes and is separate from the four role Apps.
[Runtime setup](docs/factory-runtime.md) covers admission, capacity and operations.
[External verification](docs/human-verification.md) covers checks a person must run
and opting a repository into verification through CI.
GitHub raw content remains authoritative; cached summaries are not independent
proof of approval or completion.

## Development

The Python runtime uses the standard library. Run:

```sh
python3 -m unittest discover -s tests -v
```

The operator UI has its own unit and end-to-end suites, in `ui/`:

```sh
cd ui && npm ci && npm test && npm run test:e2e
```

`npm run test:e2e` drives a real Chromium through `playwright-core`; install
it first with `npx playwright-core install chromium`, or set
`FORGE_BROWSER_PATH` to an existing executable. Installing Chromium's system
dependencies may require administrator access. See
[`ui/README.md`](ui/README.md) for the harness and its environment variables.
GitHub Actions runs the Python suite and the `ui` job on pushes and pull
requests.

These checks use synthetic local fixtures and do not mutate GitHub. Optional systemd integration tests are documented separately and
must be enabled explicitly.

## Source layout

| Directory | Purpose |
| --- | --- |
| `forge/` | Setup/update, settings, the setup supervisor, and the durable factory (`forge/factory`) |
| `forge_client/` | Local CLI for installation, host maintenance and scoped agent connections |
| `ui/` | Operator UI (Nuxt) |
| `deploy/` | Runner image, isolation policies and deployment utilities |
| `docs/` | Operator and reference documentation ([index](docs/README.md)) |
| `tests/` | Deterministic backend and browser regression checks |

Read [CONTRIBUTING.md](CONTRIBUTING.md) before contributing and
[SECURITY.md](SECURITY.md) for security reporting and operational boundaries.

## License

MIT. See [LICENSE](LICENSE).
