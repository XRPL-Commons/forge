# Running the durable factory — reference

Technical runtime reference for the selected SQLite runtime. Start with [Workers, accounts and merging](factory-runtime.md).

For lifecycle diagrams, waiting reasons and operator actions, see the
[issue state machine and blockers](issue-state-machine.md).

The implementation lives in `forge/factory`; it is Forge's only operating mode. The operator UI is a separate
`forge-ui` service on the tailnet address, and `forge-status` serves only the JSON
API on localhost; see [UI deployment](ui-deployment.md). Settings edits versioned
configuration through the selected storage authority, with validation and conflict
detection. `factory.json` and `runtime.json` are logical configuration domain
names in SQLite, not mutable files.

Queue, Pause, Return to backlog, Release hold, Retry and Recheck readiness apply
through durable, revision-checked decisions in the selected store. The API returns
the updated issue; the UI does not wait for a supervisor cycle. Decisions require
a current revision and are recorded in issue history. Pause
can be accepted while work is active: it fences the assignment immediately, then
waits for cleanup and verified GitHub synchronization. Actions that require an
idle issue remain unavailable until work and cleanup finish.

The supervisor prioritizes these issues on its next observation, verifies live
repository access, dependencies, existing work and review rules, and synchronizes
the ready/hold labels. No worker can reserve an issue until this succeeds. A
GitHub outage leaves the chosen state visible and prevents dispatch; a failed
prerequisite puts the issue on hold with its reason. A decision version prevents
older supervisor observations from overwriting a newer operator choice.
Reviewer requests and environment overrides retain their durable action flow.
Imported pending decisions without current authorization are preserved as
interrupted after a fresh GitHub read; they are not replayed. Review the current
issue, then resubmit the intended action or explicitly resume its migration pause.

The [security guide](../SECURITY.md#isolation-status) holds the canonical
live/staged/retired inventory. Supported `bin/forge` commands enter
`forge.launch`; the setup service selects the SQLite controller after cutover.
A saved `legacy` onboarding phase becomes `setup`, never a tmux supervisor.
Existing evidence: [launcher routing](../bin/forge),
[service selection](../forge/setup_service.py), and the
[retired-phase regression](../tests/test_onboarding.py).

## Configuration

Use guided Settings or the supported CLI to configure the instance. Fresh CLI installations
initialize selected SQLite before starting services. Use
[fresh-host setup](cli-fresh-host.md) for a new VM, or `forge server setup` to
configure identity and HTTPS on an existing host.

- GitHub connections and selected repository IDs belong to the `factory.json` domain.
- Runner image, policy references, capacity, deadlines, human reviewers and merge
  controls belong to the `runtime.json` domain. Setup has separate staged domains.
- Credentials stay in private referenced files; configuration stores references.
- Selected lifecycle, configuration, identity, assignments and operator requests
  live in the private SQLite store located by `state/storage`, outside workers.
  Back it up coherently with instance metadata and referenced credentials. Do not
  copy only a live database's main file or use the frozen JSON capture as a backup.

The database uses WAL with FULL durability. The launcher validates the selected
instance, schema and qualified SQLite runtime; failure does not restore JSON
access. SQLite qualification applies to the library linked to the service
interpreter. Repair it through `bin/forge setup initialize`; do not bypass the launcher
or delete `state/storage` to regain access. Never commit runtime state, App keys
or provider authentication.

Defaults (`forge/factory/config.py`) are 4 Build, 2 Review, 1 Audit and 1 Recovery
slot, with 4 GiB and two CPUs per job. Role slots are upper limits. Total concurrency is automatic:
before reserving work and immediately before starting a container, Forge checks
host MemAvailable, memory pressure and the full memory limits of existing Forge
containers. It retains 2 GiB for the host, never treats swap as available RAM,
and does not let aggregate worker budgets exceed physical RAM minus that reserve.
Created, stopped and orphaned Forge containers keep their reservation until removed.
Container starts are serialized per engine socket, including setup probes.
Missing host or engine telemetry postpones new launches. Existing workers continue;
waiting does not consume the issue failure budget or its execution timeout.

An 8 GiB host with 4 GiB workers can normally admit only one worker. Increasing
role limits cannot override available memory. Larger hosts can admit more workers.
Operators configure only the per-role limits; a `max_running` total is refused.
This is admission control, not a reservation against unrelated host processes:
external processes may still grow after the check. Per-container hard limits remain.

Adaptive memory is opt-in: set `memory: "2g"` and
`memory_tiers: ["2g", "4g", "6g"]`. An empty list retains fixed sizing.
Every tier remains a hard container limit and is reserved in full. A container
OOM kill, or a validated `blocked_environment/resources` result identifying heap
or memory exhaustion, retries the same stage at the next tier. Generic resource
failures, timeouts and text appearing only in logs do not escalate. Memory retries
consume no coding-failure allowance and do not create code investigations. At the
largest tier the issue stays on an environment hold for operator attention.

The selected store remembers successful allocations by repository ID and role. Existing
memory-failure evidence also prevents retrying that issue at a known-small limit.
This is a role-level approximation of workload type: a review may run a full build.
A larger pending retry takes precedence over new small jobs; workers already
running finish normally. Deployment investigators also defer. An allocation that
cannot fit even on an empty host is held, rather than blocking the whole queue.
Capacity and pressure checks still apply, and role limits are unchanged.

Retries keep the candidate, pipeline stage and build branch. If a build pushed
work before reporting memory exhaustion, the new harness checks out that remote
branch, including commits newer than the last accepted candidate. Unpublished
changes in a disposable container cannot be recovered after its removal. Deploy
the updated harness image before enabling tiers. Changing runtime policy does
not resize or interrupt existing containers. The selected store retains memory profiles and retry evidence; include that
history in the coherent state backup.

Build times out after 45 minutes, Review/Audit after 20, Recovery after 10.
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

`discussion_model` (default `null`) optionally names a faster model per provider
for issue conversations only, for example `{"claude": "claude-haiku-4-5"}`.
Providers it does not name, and every pipeline role, keep the pool entry's model.

Credentials stay in the private installation store. Refreshable OAuth credentials
are serialized per reference and saved using compare-and-swap before teardown;
API-key and long-lived-token jobs retain full configured concurrency. Refreshed
credentials never replace a newer manual login or mutate an external imported login file.

Set `codex_parallel_auth: true` in runtime settings (`runtime.json` domain) to share an existing ChatGPT
subscription across Codex workers. The controller gives each worker an access-only
`chatgptAuthTokens` snapshot, without the refresh token. It checks that the access
token lasts beyond the job deadline plus five minutes. When needed, the pinned
Codex app-server renews the login under the installation credential lock, in a
bounded container with no repository, no model turn, and a private durable auth
directory. Interrupted renewal is reconciled before another refresh is allowed.
Snapshots are never written back into the installation login.

Workers started with refresh authority must finish cleanup before parallel workers
start. Disabling the setting likewise waits for existing jobs before returning to
single-writer mode. Role slots, provider quota cooldowns and host memory admission
still apply. This uses ChatGPT subscription entitlement, not an OpenAI API key.
Revoked access still requires reconnecting the account. Qualify Codex with
`bin/forge setup check codex --root ROOT` after enabling this mode.

## Image and host policy

Build from the repository root:

```sh
docker build -f deploy/runner/Dockerfile -t forge-runner:local .
docker image inspect forge-runner:local --format '{{.Id}}'
```

Set the returned immutable `sha256:...` image ID through runtime Settings. The adapter manifests
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
through the separate HTTPS CONNECT proxy; no host port is published. Setup and
update start it as the `forge-egress-1` container (Compose project `forge`, from
the copy under `<runtime-root>/deploy/runner/`), running as the service account's
uid, and wait for its health check. `docker ps` should show it `healthy`.

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
approving reviews, stale-approval dismissal, and a controller App bypass through
pull requests. A known missing controller bypass blocks setup and dispatch.
Repository owners choose all
other protection settings, including required CI, administrator enforcement,
bypass actors and push permissions. No exclusive Controller access, global
Forge status-check requirement or strict/up-to-date setting is required.

Forge publishes and independently verifies its own App-bound review/audit checks
for each accepted candidate. Other contributors' approvals cannot stand in for
those technical roles. The supervisor checks the shared review baseline before
dispatch and merge. The reader combines classic branch protection and active rulesets, including
inherited organization rules. GitHub may hide bypass actors; their review remains
an operator responsibility. Other repository constraints still apply; a review
baseline pass does not certify all merge requirements or merge-queue support.

External merges are observed as completion and any active runner is cleaned up.
Target-branch changes invalidate prior evidence and restart review. A PR changed
outside its build assignment is paused for reconciliation, preserving the
contributor's changes without charging a failed attempt or dispatching a builder
over that work. Existing developer PRs require explicit adoption; Forge does not
claim them by matching a title or a conversational link.

**Who can approve.** By default, any person with write access to the repository
can approve an escalated change, as on GitHub. Settings can restrict approvals to a
list of GitHub logins (`human_reviewers`). The issue panel lists each review Forge
found and whether it counted. Forge re-reads approvals immediately before merging,
and the merge is pinned to the approved head commit.

Human approval must follow acceptance of the current candidate and still be
approved on its exact head. Bots and the PR author do not count. Head, base,
requirements or policy changes invalidate earlier candidate evidence. GitHub's
own protections make the final decision; the merge request includes the expected
head SHA through GitHub's ordinary merge API, using the controller's configured
pull-request bypass for the review baseline.

## Deployment observation

The strategist role reads an enabled repository once it has no deployment
profile, and again whenever the digest of `.github/workflows/*` on the default
branch differs from the one recorded for its profile — checked at most every
30 minutes — or when the operator presses "Re-read deployment" on the
Repository page. It runs under its own internal slot, `deployment-strategist`,
one execution at a time across every repository: admission follows the same
host memory check as any other job, and a job waits a cycle when the provider
credential its runner config points at is already leased by pipeline work. A
strategist job never has a candidate and never publishes; its checkout is the
default branch at the observed head, and it uses the same reduced read-only
token Recovery jobs get (`metadata`, `contents`, `issues`, `pull_requests` at
`read`), not a fifth App.

Each supervisor cycle, inside the existing GitHub budget, observation reads
every confirmed profile: the deployments API for the profile's environment,
and, when the profile's exposure and domain are set, one HTTPS request to the
domain — 5 second timeout, response capped at 64 KiB, no redirect across
hosts, no credentials sent. A domain that fails three cycles in a row is
retried every 10 minutes instead of every cycle. Errors reading a domain or
the deployments API are recorded per repository and surfaced as a warning,
never as a false "not deployed".

`deployments: read` on the Controller App is recommended, not required.
`ROLE_PERMISSIONS` does not include it; observation asks for it as an optional
extra on top of the controller's ordinary token, and an installation that has
not granted it still gets domain reads — only the GitHub deployments half of
observation is unavailable. The GitHub access page lists it among the
controller's missing recommended permissions until granted.

## Qualification and activation

Run the regression suite first:

```sh
python3 -m unittest discover -s tests -v
```

Run the real App/authenticated workflow on a selected pilot repository before
expanding access.

Guided setup's Start activates the factory; the selected store's `factory_active`
runtime control records activation. The setup supervisor (`bin/forge service`) runs
the factory cycle and holds `state-supervisor.lock`. Do not operate two separate
runtime roots against the same repositories: the file lock is local, not a
distributed claim.

```sh
bin/forge settings validate --root /var/lib/forge
bin/forge settings sync --root /var/lib/forge
```

Keep `enabled` and `merge_enabled` false until the image, access and pilot are
ready. Enabling a repository does not change those runtime switches. Existing PR
associations found during first discovery are held for an explicit decision;
earlier labels and rejected-cycle counters are never silently imported as current
pipeline facts. Human retry retains the old findings and starts Build.
It does not delete or guess ownership of an existing PR.

## Operations and limitations

Use the card's Stop/Reset control for an execution and Retry for a held/failed
issue. Each action has an explanatory confirmation and records a reason. Reset
never deletes GitHub publications.

Three failures across the issue stop automatic work. Investigations still run
when the third failure is reached. Two matching failure patterns require a concrete,
evidence-supported alternative; absent one, the issue is held before another try.
An investigator's classification is a diagnosis, not proof: uncertainty stays in
its record, and the supervisor independently validates any recovered publication.

The selected runtime uses local SQLite transactions, not whole-ledger JSON
rewrites. Missing or corrupt selected state fails closed. Losing that state
requires explicit execution and GitHub reconciliation before reinitializing;
do not create a blank database over running work. Recovery proceeds forward;
the pre-migration JSON copy is diagnostic only.

The supervisor prunes eligible raw logs after 30 days, retaining active work,
current-candidate evidence, recovery/cleanup and pinned references. Events and
structured results are kept. Expired log metadata remains identifiable. Captured
output is bounded and redacted on a best-effort basis, but remains private
operational data. See [security and backup boundaries](../SECURITY.md).

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


Claude launches from `/opt/forge/claude-control`, an image-owned directory with
empty sandbox mount placeholders prepared at build time. Its checkout is an
added working directory; the credential-scrubbing mode stays enabled. This keeps
launch-local package manifests, lockfiles and executable links protected without
making the repository's dependency installation read-only. The Claude readiness
probe performs offline npm install/ci and pnpm install/frozen-install, executes a
fixture's bin link and lifecycle script, verifies Git writes, and checks credential
and filesystem isolation inside the actual Bash sandbox.

The scheduler honors same-repository `Depends on`, `Blocked by` and `Dependency:`
statements on individual issue-body lines. A following `Blocks:` clause describes
downstream work and is excluded. It verifies prerequisites are closed on GitHub
before reserving a worker, rechecks waiting issues automatically, and does not
consume their code retry budget. General references, cross-repository references,
and fenced examples are not dependency declarations. Existing operator/repeated
failure holds still require their recorded decision; this does not erase them.

Exhausting a controller cycle's GitHub budget defers the existing launch intent;
it does not create a CI or repository-protection hold. Actual CI-read failures
retain the candidate and stage and are rechecked with the availability retry
interval. Discovery has a bounded share of the cycle so dispatch retains time.

### Issue ownership and handoff

The supervisor and issue panel use the same ownership check. A human assignee,
`wip` label, or open pull request that claims the issue (for example, `Closes #12`)
pauses new work. A casual mention of the issue does not claim it. Bot assignees do
not take ownership. Forge recognizes its own candidate and verifies the author
and repository of earlier Forge drafts; a branch name alone is insufficient.

Activity shows the owner and the handoff instruction before dependency waiting.
Forge checks again automatically. Removing the assignment or `wip` label, or
closing/unlinking the competing PR, lets the issue continue once its other gates
pass. The wait preserves the candidate and retry budget. Already running workers
finish their current step; the supervisor blocks the next dispatch or merge.

A reviewer assigned by Forge's recorded human-review action is still a reviewer
while that candidate awaits approval or merge. That assignment does not transfer
implementation ownership. Other assignees continue to block new work.


### Repository merge policy

The selected runtime configuration stores `repository_merge_policies` as a map
from immutable GitHub repository ID strings to `human` or `auto`. Missing IDs
mean `human`. The old global `human_required` field is retained for legacy import
and the retired JSON supervisor; live SQLite admission uses the repository map.
The global `merge_enabled` switch still gates every merge.

`repository_merge_check_scopes` maps the same repository ID strings to `all`
(the default when missing) or `forge`. With `all`, Forge publishes
`forge/merge-policy` on every open pull request: a PR Forge did not create
passes it (Forge review and audit do not apply; the repository's own reviews,
CI and signing rules still decide), while a Forge-created PR passes only through
the merge controller's verified gates. A commit shared with a Forge candidate
keeps the merge controller's verdict. The downloaded baseline ruleset then also
requires `forge/merge-policy` from the Forge App. With `forge`, human PRs carry
no Forge check and the ruleset does not require it. The scope is outside the
candidate policy hash, so changing it never withdraws a pending merge.

`policy(config, repository_id, repository_name=...)` returns a candidate policy
bound to the repository ID, its effective mode, human reviewer eligibility,
merge method and only that repository's verification checks. Candidate hashes
exclude unrelated repository policies. Dispatch, result acceptance, verification
and fresh merge authorization must use this same function.

Schema 25 and legacy import normalize existing settings consistently. An enabled
legacy auto setting is copied only to IDs present in the matching configured
repository document. Explicit per-repository entries win; new IDs default to
human. Schema upgrade appends a configuration revision and retains prior content
and credential references.

The CLI's `/api/client/policy` write requires the separately approved
`repositories:policy` capability. It compares the runtime revision and commits
the configuration revision, attributed audit record and idempotent operation
receipt together. Grant revocation is serialized against that effect. Read-only
connections can inspect policy. Browser Settings and issue details display it;
neither exposes a policy editor.

### Measured worker resources

The lazy delivery tree exposes timestamped CPU/current-memory samples and verified
terminal wall duration per physical execution. See [measurement meanings and
availability](execution-resources.md); configured limits are not measured usage.

### Forge startup problems

The runner recognizes a narrow, trusted class of Forge failures before an agent
starts: import, name and syntax errors in image-owned harness code. A private pipe
connects the startup wrapper to the image entrypoint. The parent consumes and
irrevocably records the agent-start marker, closes that channel, then acknowledges
it before the harness can call the provider. Expected configuration and checkout-preparation failures,
application test failures, provider errors, timeouts and worker-written logs do
not establish this classification.

After verified capture and cleanup, a current assignment receives a
`forge_problem` gate owned by Forge. It does not consume an issue failure charge
or create a recovery investigation. The gate retains the attempt, actual image,
exception class, image file/line and stable signature; the existing scrubbed log
artifact retains diagnostics. Replayed results do not create another gate or
charge. Concurrent failed tasks share one unit blocker; immutable job, member and
gate associations retain each separate captured result, including after an
operator splits the delivery. Other holds and assignment
fences remain effective.

The normal supervisor wait recheck releases this blocker when every affected
standard/full image has a different active immutable pin matching the updater's
successful qualified host record. Changing configuration alone does not release
it. Independent holds, stored work and the failure budget remain unchanged; the
ordinary scheduler then decides whether work can resume. The qualified image may
already be installed when an older job finishes. Source-only or same-image
updates do not retry the same startup fault.

This first step does not file GitHub issues, correct historical charges without
trusted receipts, or classify all runner/store failures. The full #262 exit envelope, provider retry and report-drafting contract remains
unimplemented. This receipt is consumed by ordinary ResultController jobs;
recovery and auxiliary planning/discussion controllers do not yet classify it.
Those parts of #258/#262 remain separate work. Images predating the startup
protocol cannot supply this proof.


## Durable scheduling and uncertain effects

Every dispatch, result and merge decline records its exact work scope, responsible
controller, reason and next try time. The default retry delay is 30 seconds. A
changed assignment invalidates its old timer; admission still checks current
facts. A future timer counts as owned work. The board calls work Queued only when
that timer exists and shows when Forge will try again.

The local supervisor audits ownership after remote phases, even when their
request budget is exhausted. Cleanup, captured results, scoped gates, candidate
effects and explicit maintenance count as ownership. After 300 seconds without
ownership, a member gets a Forge problem with its last decline. Recheck schedules
a fresh attempt without charging or clearing independent holds.

Candidate effects persist a retry deadline before possible delivery. Exponential
backoff starts at 30 seconds, caps at 900 seconds, and stops sends after eight
attempts. Unknown outcomes older than 900 seconds get an owned Forge problem;
read-back continues. These bounds are configured in the runtime `reliability`
object. An attempt is an operation attempt, including its bounded subrequests;
read-only observations and budget-deferred reads do not consume send attempts.

A fresh exact merge observation completes the merge. A closed-unmerged PR or a
changed head observed after the last send reconciles all earlier uncertain sends
and atomically returns the candidate for rework. HTTP refusal remains a separate
proof restricted to the first send. Observed conflicts sustained for 900 seconds
rebuild only when no merge was possibly sent. Unknown mergeability remains
uncertain. Neither elapsed time nor an HTTP success proves a merge.
