# Install a new Forge VM

Use a Linux VM with Python 3.10+, Git, Docker Engine with the Compose plugin,
and systemd. Run Forge as a dedicated non-root service account with Docker
access. Follow [Docker's installation instructions](https://docs.docker.com/engine/install/)
for your distribution. Docker access is administrative authority; never give it
to a worker. Enable lingering for that account if its user services must survive
logout (`sudo loginctl enable-linger <service-account>`).

```sh
git clone https://github.com/XRPL-Commons/forge.git
cd forge
bin/forge setup
```

Keep this checkout in place: the installed services run its reviewed source.
Setup builds the pinned runner image containing Codex and Claude Code, installs
the runner's isolation policy and private egress network, and starts the dashboard
and supervisor. Installing the AppArmor profile may prompt for sudo. It does not
start issue processing or alter GitHub permissions.

The wizard offers each coding account in turn. Each login runs the installed
harness's official login command in a temporary container, without any repository
or GitHub credentials. Follow the link/code instructions in your terminal using
your browser. Skip either account if desired. Forge privately stores the resulting
login under the service account's runtime directory; no secret belongs in Git,
command arguments, or browser onboarding.

Setup then runs a small real model request for each configured model, verifies
native tool isolation, and checks termination and runner reuse. Provider charges
may apply. An unavailable account is excluded while other verified accounts remain usable.
If none pass, the factory stays stopped. Cleanup intent is always retained.

Open `http://127.0.0.1:8787/onboarding` to configure GitHub Apps, choose repositories,
verify repository protection, and explicitly start Forge. From your computer,
forward the port with `ssh -L 8787:127.0.0.1:8787 <service-account>@<vm>`.
The UI has no per-user authentication: bind only to loopback or your trusted
tailnet (`--bind <tailnet-address>`).

## Resume or reconnect

The default private runtime is `~/.local/share/forge`. Use `--root` consistently
if choosing a different directory. It must be owned by the service account and
have mode 0700. Existing host settings and credentials are preserved when setup
is repeated. Reset is an explicit separate operation in the UI.

```sh
bin/forge setup install                 # Prepare the host without interactive login
bin/forge setup login codex             # Device-code login
bin/forge setup login claude            # Claude Code's browser/code login
bin/forge setup check                   # Verify all configured harnesses
bin/forge setup status                  # No credential values printed
```

Both login commands support `--api-key` to prompt privately instead of using an
account login, and `--import-login /absolute/path` for an existing login file.
Import copies it; Forge never writes back to the source. Reconnect through Forge
after import so another application cannot rotate the same credential separately.

Repeat `--model <model-id>` on login to configure multiple models. Without it,
the harness chooses its default model. Provider/model entries have equal weight
in one durable round-robin sequence across all roles. Auth or quota failures
temporarily skip that harness and fall back to another without consuming the
issue's failure budget. When all entries are unavailable, issues wait.

After an active factory reconnects, that harness remains excluded from scheduling
until `setup check` succeeds. Verification drains running jobs before probing.
The `/settings/harnesses` page shows configured models, verification status, and
the appropriate terminal reconnect command.

Refreshable account logins have one active writer per credential reference. Forge
saves refreshed credentials before removing the runner and refuses stale writes
after a newer login. API-key jobs can use the full configured parallel capacity.
Losing a refresh during a crash can still require another login; cleanup and other
available harnesses continue independently.

For host requirements, isolation qualification, capacity and upgrades, see
[the runtime guide](factory-runtime.md). To add a harness, see
[the adapter contract](harness-adapters.md).

Official login references: [Codex authentication](https://learn.chatgpt.com/docs/auth)
and [Claude Code CLI](https://code.claude.com/docs/en/cli-reference).
