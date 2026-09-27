# Forge

The durable factory implementation is available through `bin/forge factory` and
`/factory`: resettable runners, supervisor-owned issue history, sequential review
and audit, mandatory failure investigations, and a shared three-failure budget.
Start with [VM installation and terminal login](docs/installation.md),
[guided onboarding](docs/onboarding.md), and the
[workflow contract](docs/software-factory-spec.md). For pipeline states and why an
issue is waiting, see the [issue state machine and blockers](docs/issue-state-machine.md). It uses an explicit cutover;
installing this source does not take over the legacy fleet.


Forge moves GitHub issues through **Build → Review → Security audit → Merge**.
GitHub holds requirements, code and review evidence. The supervisor owns the durable
issue ledger, assignments, failure history and resource allocation. Workers receive
one assignment and run inside disposable containers; completion is verified against
GitHub before the issue advances.

**Status: experimental.** The durable factory is implemented and independently
selectable. It has synthetic workflow/restart tests and real credential-free runner
canaries. GitHub App installation and repository protection still require owner setup before a live run.
The guided setup verifies access, provider authentication and disposable runner cleanup before Start. Human GitHub App approvals still happen on GitHub. Existing tmux fleets remain active until an explicit reset.
See [implementation status](docs/implementation.md).

The following observation and monitoring instructions describe the compatibility
path. New installations should begin with the runtime setup linked above.

## Start with observation

Use Linux, Python 3.10 or newer, Git, and the GitHub CLI (`gh`). Authenticate `gh`
with access to the repositories you want to observe.

```sh
git clone https://github.com/XRPL-Commons/forge.git
cd forge
mkdir -p local/repos.d
cp examples/repos.d/example.repo.example local/repos.d/project.repo
# Edit project.repo: repository, ownership label, bot identity, and clone paths.
bin/forge shadow --config ./local/repos.d --state ./local/state
```

The example starts disabled. Before enabling a repository, deliberately provision
its configured `auto:owned-by-*` ownership label. Shadow mode reads queues and
writes local snapshots; it does not launch workers, change labels, or merge PRs.
Set `RECOVERY_AUTHOR` explicitly to the GitHub identity used by your workers.

Legacy live execution additionally needs tmux, supported coding-agent CLIs, separate
worker clones, and an operator-managed service installation. The files in
`deploy/` are templates and containment utilities, not a one-command production
installer. Configure paths, agent identities, credentials, and merge policy for
your own installation before starting a supervisor. Never copy another fleet's
operational state or credentials.

## Dashboard and history

The [GitHub settings guide](docs/github-settings.md) covers the `/settings` page,
human-managed App installation, repository discovery, and explicit activation.
Start with `examples/factory.json.example`: it has no credentials or enabled
repositories. Settings are editable in the UI or as a file; secrets remain outside
the source tree. The durable factory issues repository-scoped role credentials;
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
| `forge/` | Queue, scheduling, activity, evidence, capacity, and lifecycle logic |
| `legacy/` | Working tmux supervisor, role instructions, and dashboard |
| `observer/` | Independent observation and isolation experiments |
| `deploy/` | Service templates and controlled deployment utilities |
| `tests/` | Deterministic backend and browser regression checks |

Read [CONTRIBUTING.md](CONTRIBUTING.md) before contributing and
[SECURITY.md](SECURITY.md) for security reporting and operational boundaries.

## License

MIT. See [LICENSE](LICENSE).
