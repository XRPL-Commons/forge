# Install a new Forge VM

Use an AppArmor-enabled Linux VM (Ubuntu 24.04 is the tested host) with Python
3.10+, Git, Docker Engine with the Compose plugin, and systemd. Run Forge as a dedicated non-root service account with Docker
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

The image also owns the build environment: Node 22, npm, Corepack (with pnpm
10.23.0 preloaded), Python 3.12, a C/C++ compiler and Chromium. You do not install
project tools on the host. Before declaring installation successful, setup runs
an offline canary in the native tool sandbox: real Node memory readings, package
manager execution, a bounded build, a browser page, a Git commit, and protection
of private credentials and agent configuration. A failure stops installation
before services or the success marker are written.

Each checkout has disposable disk-backed temporary and package-cache directories.
Workers install the repository's locked dependencies inside their native sandbox;
Forge does not execute repository installation scripts in the credential-bearing
parent process. Corepack honors a project's declared package-manager version;
versions not preloaded require allowed registry access. Browser tests can use
`PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH` or `CHROME_BIN` to select image Chromium.

Inside a worker, `safe-build COMMAND` serializes builds, limits Node's heap to
60% of the container's finite memory budget, and kills the process group after
the deadline (20 minutes by default, at most 40). The container cgroup remains
the hard resource limit; the host admission check reserves memory before launch.
This is independent of the host's systemd-based `bin/safe-build` helper. Projects
needing another runtime or more memory stop with an environment blocker; Forge
does not bypass isolation or treat missing infrastructure as a code defect.

## Qualify an updated runner

Existing installations keep their pinned image when `setup install` is repeated.
For an upgrade, build the reviewed checkout using the `NODE_IMAGE` and
`PYTHON_IMAGE` digests in `forge/host_setup.py`, then run:

```sh
python3 deploy/test_build_environment.py --image <new-image-id> \
  --seccomp <runtime-root>/runner-seccomp.json \
  --apparmor forge-runner-private-proc
```

The same private-process AppArmor policy is mandatory for qualification and live
workers; unsupported hosts fail installation explicitly. Keep dispatch paused while
updating the image references in host/runtime and saved setup configuration, then
run `bin/forge setup check --root <runtime-root>` before resuming. Never mutate an
old image in place or copy a host dependency directory into a worker.

An environment blocker preserves the published PR, stage and code-failure budget.
Deploying a different image or resource configuration permits a new readiness
check. After repairing access or connectivity without changing the image, use
**Recheck readiness** on the affected issue. A repeated blocker in the same
unchanged environment stays paused. Genuine test failures remain code failures.

The controller supplies workers with bounded CI check summaries and annotations
for the exact candidate head and base. Worker credentials remain narrowly scoped;
they do not gain Actions-log or check-publication permissions. Missing CI evidence
is explicit, and snapshots never replace GitHub's current merge requirements.

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

Port 8787 is served by the Nuxt/Nitro operator UI (`ui/`), which proxies the
Python status API on loopback. To build and switch over that front door on
an existing host, see [the UI deployment runbook](ui-deployment.md).

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

Sign-in can run while the factory is active. Forge saves the new login privately,
then waits up to two minutes for the current supervisor cycle before applying it.
If the supervisor stays busy or the terminal closes during this handoff, rerun
the same login command: Forge resumes the saved sign-in without opening another
browser login. A factory reset discards pending sign-ins from the old setup.

Repeat `--model <model-id>` on login to configure multiple models. Without it,
the harness chooses its default model. Provider/model entries have equal weight
in one durable round-robin sequence across all roles. Auth or quota failures
temporarily skip that harness and fall back to another without consuming the
issue's failure budget. When all entries are unavailable, issues wait.

After an active factory reconnects, that harness remains excluded from scheduling
until `setup check` succeeds. Verification drains running jobs before probing.
The `/settings/harnesses` page shows configured models, verification status, and
the appropriate terminal reconnect command.

`setup check` reports its current phase and elapsed time. On an active factory it
waits for existing workers to finish before using shared account credentials;
new dispatch pauses during verification. With the supervisor running, Ctrl+C
only stops watching: the check continues on the server. Run the same command to
reattach, or `setup status` to see what it is waiting for. The terminal stops
watching after 15 minutes without cancelling the saved operation.

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
