# Forge

Forge coordinates coding agents across GitHub repositories and shows each issue
moving through **Build → Review → Security audit**.

GitHub holds the issues, pull requests, reviews, and audit evidence. Local queue
snapshots, activity events, and SQLite history power the operator dashboard.
A durable local state machine tracks each issue, its exact execution assignment,
and the worker slot that owns it. Reservations and generation checks prevent
independent workers from taking the same assignment.

- Issue cards show what is working, waiting, or needs a human decision.
- Workers start on demand and release their slots after verified handoffs.
- Capacity checks account for memory, pressure, and per-role limits.
- Critical changes require human approval. Failed audits return work to Build.
- A unified activity log records dispatches, progress, releases, and blockers.

**Status: experimental.** The tmux supervisor is the working execution path.
The newer isolated executor and policy modules are staged components; live
repository execution through those adapters remains disabled. See
[implementation status](docs/implementation.md).

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

Live execution additionally needs tmux, supported coding-agent CLIs, separate
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
the source tree. This setup layer does not yet replace the legacy harness's shared
GitHub authentication with per-role execution credentials.

The dashboard is implemented in `legacy/dashboard.html` and
`legacy/forge-status.py`. It reads local state and refreshes automatically. Its
repository drawer groups pull requests, reviews, and security audits beneath
issue cards. The server exposes local worker output; keep it on a trusted private
network. It has no per-user application authentication layer. Slot controls use
a per-process capability token and require the exact bound address and same-origin
JSON requests; only the supervisor executes them. See the
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

Browser checks use the pinned Playwright dependency and Chromium:

```sh
npm install --ignore-scripts --no-package-lock --no-audit --no-fund
node node_modules/playwright-core/cli.js install --with-deps chromium
FORGE_PLAYWRIGHT_MODULE=playwright-core npm run test:browser
```

Installing Chromium's system dependencies may require administrator access.
To use an existing browser instead, set `FORGE_BROWSER_PATH` to its executable.
GitHub Actions runs both Python suites on pushes and pull requests. The optional
browser job can be enabled when manually running the CI workflow.

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
