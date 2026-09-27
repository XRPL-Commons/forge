# Forge

Forge moves GitHub issues through **Build → Review → Security audit → Merge**.
GitHub holds requirements, code and review evidence. The supervisor owns the durable
issue ledger, assignments, failure history and resource allocation. Workers receive
one assignment and run inside disposable containers; completion is verified against
GitHub before the issue advances.

## Install

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

**Status: experimental.** The durable factory has synthetic workflow/restart tests
and real credential-free runner canaries. The guided setup verifies access, provider
authentication and disposable runner cleanup before Start; GitHub App approvals
still happen on GitHub. See [implementation status](docs/implementation.md).
Existing tmux fleets are a separate compatibility path described in
[legacy operation](docs/legacy.md); installing this source does not take them over.

## Dashboard and history

Guided onboarding creates the GitHub Apps and configuration. The
[GitHub settings guide](docs/github-settings.md) is the reference for the `/settings`
page, repository discovery, explicit activation and the underlying files; secrets
remain outside the source tree. The durable factory issues repository-scoped role credentials;
the compatibility supervisor retains its existing authentication.

The operator UI is the Nuxt app in `ui/` (service `forge-ui`); it reads local
state through `legacy/forge-status.py`, which now serves only the JSON API on
loopback. `bin/forge setup` installs both and `bin/forge update` keeps them current;
see [`docs/ui-deployment.md`](docs/ui-deployment.md). The pages refresh automatically. The server exposes local worker
output; keep it on a trusted private network. It has no per-user application
authentication layer. Slot controls use a per-process capability token and
require the exact bound address and same-origin JSON requests; only the
supervisor executes them. See the
[monitoring guide](docs/monitoring.md) for Stop, Resume, and Release behavior.

See [observability](docs/observability.md), [activity](docs/activity.md),
[workflow and slots](docs/workflow.md), [handoffs](docs/handoff.md), and [capacity](docs/capacity.md) for the data model and
operational behavior. GitHub raw content remains authoritative; cached summaries
and observed transitions are not independent proof of approval or completion.

## Development

The Python runtime uses the standard library. Run:

```sh
python3 -m unittest discover -s tests -v
python3 -m unittest discover -s observer/tests -v
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
GitHub Actions runs the Python suites and the `ui` job on pushes and pull
requests.

These checks use synthetic local fixtures and do not mutate GitHub or send
notifications. Optional systemd integration tests are documented separately and
must be enabled explicitly.

## Source layout

| Directory | Purpose |
| --- | --- |
| `forge/` | Factory supervisor, setup/update, scheduling, evidence and lifecycle logic |
| `ui/` | Operator UI (Nuxt) |
| `legacy/` | Status API (`forge-status.py`) and the compatibility tmux supervisor |
| `observer/` | Independent observation and isolation experiments |
| `deploy/` | Runner image, isolation policies and deployment utilities |
| `tests/` | Deterministic backend and browser regression checks |

Read [CONTRIBUTING.md](CONTRIBUTING.md) before contributing and
[SECURITY.md](SECURITY.md) for security reporting and operational boundaries.

## License

MIT. See [LICENSE](LICENSE).
