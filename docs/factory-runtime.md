# Running the durable factory

The new implementation lives in `forge/factory`. It is selected explicitly; installing
this source does not take over the legacy fleet. Use `/factory` to inspect the new
ledger before cutover. After cutover, `/` serves that view. Settings edits
`factory.json` and `runtime.json` with validation and conflict detection.

## Configuration

Copy `examples/factory.json.example` and `examples/runtime.json.example` into your
private runtime directory. Both start with automation disabled. Never commit these
copies, App keys, provider authentication, or `state/`.

- GitHub connections and selected repository IDs belong in `factory.json`.
- Runner image, policy references, capacity, deadlines, human reviewers, and merge
  controls belong in `runtime.json`.
- Credentials stay in private server environment variables or referenced files.
- Issue history, assignments, runner ownership and operator requests live under
  `state/factory/`, outside every disposable workspace. Back up that directory.

Defaults are four Build, two Review, one Audit and one Recovery slot; eight jobs
maximum, 4 GiB and two CPUs per job. Adapt the shared cap and memory limit to the
host. Build times out after 45 minutes, Review/Audit after 20, Recovery after 10.
The graceful-stop limit is 10 seconds. The supervisor polls every 30 seconds and
budgets GitHub reconciliation to 60 seconds plus the current bounded request.
Outstanding observations rotate between cycles. Access sync runs asynchronously;
cleanup never waits for an access refresh. Running output is sampled with a bounded
read and shown by Output; it cannot establish completion. Only a finished execution
with independently verified results can advance an issue.

Codex and Claude Code implement the [same adapter contract](harness-adapters.md).
Their manifests pin the distribution and version, terminal login, and credential
storage. New VMs start with [terminal setup](installation.md).

`provider_pool` contains equally weighted provider/model entries. Selection rotates
across all roles and survives controller restarts. Auth/quota failures cool down
that provider and use another without consuming the issue failure budget. Empty
pools preserve the legacy single-provider configuration. Explicit `provider_ready`
entries fence unverified reconnects until qualification completes.

Credentials stay in the private installation store. Refreshable OAuth credentials
are serialized per reference and saved using compare-and-swap before teardown;
API-key jobs retain full configured concurrency. Refreshed credentials never
replace a newer manual login or mutate an external imported login file.

## Image and host policy

Build from the repository root:

```sh
docker build -f deploy/runner/Dockerfile -t forge-runner:local .
docker image inspect forge-runner:local --format '{{.Id}}'
```

Put the returned immutable `sha256:...` image ID in `runtime.json`. The adapter manifests
pin both coding harnesses; for a reproducible production build also pin `NODE_IMAGE` and
`PYTHON_IMAGE` by digest. `.dockerignore` admits only the runner source and image
policy, so a build cannot accidentally send runtime credentials to the daemon.

The container uses a read-only root, UID 1000, no Linux capabilities, no new
privileges, a private PID namespace, an owned anonymous workspace volume, and
bounded memory/process/log resources. It never receives host directories, App
private keys, a controller token, or the Docker socket. The trusted controller's
Docker access is administrative authority: keep that account separate from worker
identities and do not expose its socket over TCP.

Codex's native sandbox remains enabled. Its image-owned
`/etc/codex/requirements.toml` limits work to the repository and denies repository
tools access to provider authentication. Shell commands receive only the assigned
repository's GitHub token and necessary public tool/proxy settings. The provider
credential remains in the harness environment. No unsafe sandbox fallback is used.

Ubuntu's AppArmor restriction and Docker's default seccomp policy can prevent
nested native sandboxing. The supplied runner-only AppArmor profile permits
nested user namespaces and mounts while retaining filesystem/kernel restrictions.
The seccomp generator adds the namespace/mount calls to Docker's default-deny
profile; it does not grant host capabilities. Install these only for the runner:

```sh
curl --fail --location \
  https://raw.githubusercontent.com/moby/profiles/3c28324314729dbade8287e868eef6338c42807a/seccomp/default.json \
  --output /tmp/docker-default-seccomp.json
python3 deploy/runner/seccomp_profile.py /tmp/docker-default-seccomp.json \
  --sha256 536529b665dd0972c37bfb569f5d4ac8a53592e7b00752bc39ff063ca9864c74 \
  --output /tmp/forge-runner-seccomp.json
sudo install -D -m 0644 /tmp/forge-runner-seccomp.json /etc/forge/runner/seccomp.json
sudo install -m 0644 deploy/runner/forge-runner-private-proc.apparmor /etc/apparmor.d/forge-runner-private-proc
sudo apparmor_parser -a /etc/apparmor.d/forge-runner-private-proc
```

The checksum pins the upstream bytes reviewed for this implementation. The URL pins the upstream commit as well. To upgrade, review the new upstream version before
updating both the URL and checksum. It does not download or apply anything itself. Existing
AppArmor policy replacement requires its own reviewed update; use `-a` for the
first installation, not a blanket reload of host profiles. Set the controller's
`FORGE_RUNNER_SECCOMP=/etc/forge/runner/seccomp.json`. Hosts without AppArmor can set
`apparmor_profile` to `null` after qualifying their own native sandbox boundary.
The runtime refuses an unavailable seccomp file before assigning an issue.

Claude Code additionally requires `forge-runner-private-proc`, installed by
terminal setup. Its nested PID namespace must mount a fresh procfs. For this
harness only, the runner replaces Docker's masked proc mounts with that dedicated
AppArmor profile's equivalent sensitive-path denials. Container PID isolation,
read-only root, dropped capabilities, no-new-privileges, seccomp and native
sandboxing remain enforced. Other profiles cannot opt into this mode. Install
the new profile separately; do not overwrite a policy used by running containers.

The image keeps Claude's system Bubblewrap off Codex's PATH so Codex uses its
own pinned sandbox helper. Each adapter must pass a real tool canary after image
or host-policy changes; a model's success claim is insufficient evidence.

## Network boundary

Workers use the internal `forge-workers` Docker network. They reach public services
through the separate HTTPS CONNECT proxy; no host port is published:

```sh
docker compose -f deploy/runner/compose.yaml up -d egress
```

The proxy permits configured domains on port 443 and rejects private, loopback and
non-global DNS answers. Its default allowlist covers GitHub, OpenAI, Anthropic, npm and PyPI.
Edit `FORGE_EGRESS_DOMAINS` on the proxy for additional package registries. An
unlisted dependency host is an execution finding, not permission to use a public
worker network. The runner refuses a non-internal network. Pin the proxy image by
digest for production as well.

## GitHub gates

Set up the four Apps as described in [GitHub settings](github-settings.md).
The controller additionally needs **Administration: read** to verify branch
protections; it does not need an administration write grant. Workers receive fresh,
repository-scoped installation tokens. Review, Audit and Recovery jobs receive
read-only tokens; the supervisor publishes reviews/checks using the corresponding
role App after validating the result. No worker receives the controller's token.

Forge operates alongside developers. The default branch baseline requires two
approving reviews and stale-approval dismissal. Repository owners choose all
other protection settings, including required CI, administrator enforcement,
bypass actors and push permissions. No exclusive Controller access, global
Forge status-check requirement or strict/up-to-date setting is required.

Forge publishes and independently verifies its own App-bound review/audit checks
for each accepted candidate. Other contributors' approvals cannot stand in for
those technical roles. The supervisor checks the shared review baseline before
dispatch and merge. The current adapter reads classic branch protection;
rulesets-only and merge-queue policy adapters remain future work.

External merges are observed as completion and any active runner is cleaned up.
Target-branch changes invalidate prior evidence and restart review. A PR changed
outside its build assignment is paused for reconciliation, preserving the
contributor's changes without charging a failed attempt or dispatching a builder
over that work. Existing developer PRs require explicit adoption; Forge does not
claim them by matching a title or a conversational link.

For automatically merging **escalated** changes, configure native required
code-owner reviews and a `.github/CODEOWNERS` file with one wildcard rule listing
only humans in `human_reviewers`, for example:

```text
* @example-maintainer
```

This is deliberately conservative: code-owner protection is repository-wide, so
it can also require humans on ordinary PRs. It closes the race in which a custom
merge-policy check remains successful after a human approval is dismissed.
Without verifiable native human protection, Forge holds an escalated PR instead of
using the earlier approval as permission. More selective CODEOWNERS/team patterns
need a separately verified policy adapter; they are not guessed.

Human approval must follow acceptance of the current candidate and still be
approved on its exact head. Bots and the PR author do not count. Head, base,
requirements or policy changes invalidate earlier candidate evidence. GitHub's
own protections make the final decision; the merge request includes the expected
head SHA and never requests a bypass.

## Qualification and cutover

Run the regression suite and credential-free container reset canary first:

```sh
python3 -m unittest discover -s tests -v
PYTHONPATH=. python3 deploy/test_factory_container.py
python3 deploy/test_factory_sandbox.py --image forge-runner:local \
  --seccomp /etc/forge/runner/seccomp.json
```

The native canary temporarily loads one uniquely named AppArmor test profile,
checks repository writes and denial of a synthetic provider file/outside writes,
and removes its container/profile afterward. Review that script before granting
its narrow root operation. It does not run a model or access GitHub. Run the real
App/authenticated workflow on a selected pilot repository before expanding access.
The reset canary builds a synthetic image, creates its own internal network, and
removes only its uniquely named resources.

Before cutover, drain and stop legacy workers, reconcile their PRs, and retain their
old state as history. Install the updated legacy supervisor guard first. Both
controllers share `state-supervisor.lock`; `state/factory-v2.enabled` additionally
prevents a legacy restart after cutover. Do not operate two separate runtime roots
against the same repositories: the file lock is local, not a distributed claim.

```sh
bin/forge factory validate --root /var/lib/forge
bin/forge settings sync --root /var/lib/forge
bin/forge factory activate --root /var/lib/forge --legacy-drained
bin/forge factory once --root /var/lib/forge
bin/forge factory run --root /var/lib/forge
```

Keep `enabled` and `merge_enabled` false until the image, access and pilot are
ready. Enabling a repository does not change those runtime switches. Existing PR
associations found during first discovery are held for an explicit decision;
legacy terminal states and old rejected-cycle counters are never silently imported
as current pipeline facts. Human retry retains the old findings and starts Build.
It does not delete or guess ownership of an existing PR.

## Operations and limitations

Use the card's Stop/Reset control for an execution and Retry for a held/failed
issue. Each action has an explanatory confirmation and records a reason. Reset
never deletes GitHub publications. CLI equivalents queue the same durable requests:

```sh
bin/forge factory stop --root /var/lib/forge --target EXECUTION_UUID --reason 'Operator cancellation'
bin/forge factory reset --root /var/lib/forge --target EXECUTION_UUID --reason 'Reset failed execution'
bin/forge factory retry --root /var/lib/forge --target REPOSITORY_ID:ISSUE_NUMBER --reason 'Describe the correction'
```

Three failures across the issue stop automatic work. Investigations still run
when the third failure is reached. Two matching failure patterns require a concrete,
evidence-supported alternative; absent one, the issue is held before another try.
An investigator's classification is a diagnosis, not proof: uncertainty stays in
its record, and the supervisor independently validates any recovered publication.

The ledger is a serialized local JSON store with atomic fsync/replace and an
initialization marker. It is not a distributed database. Missing/corrupt initialized
state fails closed. Losing the entire state directory requires explicit execution
and GitHub reconciliation before reinitializing; do not create a blank ledger over
running work. History has no automatic deletion policy in this version, so monitor
its size and back it up. Captured output is bounded and redacted on a best-effort
basis, but remains private diagnostic data rather than public source material.

A verified merge or external issue closure clears current attention even when old
failure findings remain. Cleanup faults appear as runner/resource work, not as a
new failure of an already merged issue. Long-unverifiable publication outcomes
hold the issue while clean runners remain available for unrelated work.

References: [Docker seccomp](https://docs.docker.com/engine/security/seccomp/),
[Docker AppArmor](https://docs.docker.com/engine/security/apparmor/),
[Codex permissions](https://learn.chatgpt.com/docs/permissions),
[GitHub protected branches](https://docs.github.com/en/rest/branches/branch-protection).

GitHub App grants are repository-scoped, not per-issue or per-branch capabilities.
The builder is instructed to write only its assigned branch/issue/PR; acceptance
verifies those identities. GitHub applies the repository owner’s permissions and
protection rules to all actors. Forge’s supervisor gates govern the merges Forge
requests, not merges by other authorized actors. A future write broker could
enforce every individual builder API operation; role prompts are not that boundary.
