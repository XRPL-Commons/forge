# Legacy operation

This page covers the compatibility path: observation of existing tmux fleets and
the legacy live supervisor. New installations use the durable factory; start with
[installation](installation.md) instead.

## Observe queues (shadow mode)

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

## Legacy defaults

The legacy supervisor defaults to three fleet sessions and one per role
(`MAX_SESSIONS`, `MAX_SESSIONS_PER_ROLE`), with conservative pressure guardrails.
These settings do not apply to the durable factory, whose capacity is described in
[factory runtime](factory-runtime.md). They will be removed with
`legacy/supervisor.sh`.

## Manual GitHub configuration

Guided onboarding registers the four role Apps through GitHub's manifest flow and
stores their keys privately. For CLI-only installations, the manual App, key
reference and `factory.json` procedure is in [GitHub settings](github-settings.md).
