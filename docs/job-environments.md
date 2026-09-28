# Job environments

The standard worker supplies build tools and a browser. The optional full worker
adds MongoDB, PostgreSQL and Redis binaries. Both use the same container isolation,
UID 1000, read-only root, dropped capabilities, seccomp policy and egress proxy.
No service is shared between jobs; database processes bind only to loopback and
write under the job's repository in `/work`.

The full image additionally allows native tools to read the single CPU topology
file `/sys/devices/system/cpu/possible`, required by MongoDB 8's allocator. This
does not grant writes, additional capabilities or access to credentials.

## Install and qualify

On an installed Forge host:

```sh
bin/forge setup full-environment --root /path/to/forge-runtime
```

This builds the optional image and runs the offline native-sandbox canary before
adding `environment_tiers.full` to runtime configuration. The canary starts all
three services, checks a Mongoose document round trip, SQL insert/select and Redis
set/get, and stops the processes. Failed qualification leaves the tier disabled.
The image is pinned by ID; default full-tier limits are 4 GB and 2 CPUs. Change
`memory` and `cpus` in `environment_tiers.full` if needed. Admission reserves the
whole selected memory limit. Adaptive memory retries can increase it further.

Once installed, `bin/forge update` rebuilds and qualifies the full tier when its
inputs change. `bin/forge setup check` also verifies its database services.
The base MongoDB image is digest-pinned in `deploy/runner/Dockerfile.full`.

## Review each repository's services

```sh
bin/forge factory environment-probe --root /path/to/forge-runtime --target REPOSITORY_ID
bin/forge factory environment-confirm --root /path/to/forge-runtime \
  --target REPOSITORY_ID --revision REVISION_FROM_PROBE
```

The first command reads dependency manifests, test setup, Compose and CI files at
one commit. It never executes repository code. It prints proposed services and
file references, then saves a proposal with no scheduling effect. Review that
output before confirming its exact revision. Confirmation writes the repository
profile to `repository_environments` in runtime configuration. No profile means
no requested services. Re-probe after the repository's requirements change.

The probe is deliberately bounded: at most 60 relevant files, 64 KiB per file;
an incomplete tree or an exceeded file count requires manual investigation. It
looks for known dependencies and service references; it is a proposal for review,
not proof that every integration requirement was discovered.

## Selection and retries

The current supervisor selects the environment before reserving a worker. It
does not use the retired triage modules. Explicit `env:standard` or `env:full`
labels win. Otherwise, a confirmed database repository uses full, except when
the title explicitly scopes the issue to a **copy-only** or **docs-only** change.
Unknown work keeps the confirmed services. Conflicting or unavailable tier labels
defer dispatch with a reason and consume no code-failure budget.

The controller records the tier, services and reason in issue history and writes
the corresponding `env:*` label. Its own label is tracked separately so it does
not become an operator override on the next cycle. Interrupted label writes are
reconciled before a job is reserved.

A worker reports a missing database as `blocked_environment`, `blocker: services`,
and a nonempty `required_services` list drawn from `mongodb`, `postgres`, `redis`.
On standard, this permits one retry on full, on the same build branch, only when
the full image exists in configuration and the repository profile confirms every
requested service. An explicit standard label prevents automatic escalation.
Unconfirmed services or a second service blocker hold the issue for an operator
to repair and **Recheck readiness**. Neither case consumes a code-failure attempt.

Inside a full-tier job, run services and tests in the same tool invocation:

```sh
forge-services npm test
```

The helper starts the requested services, runs the command with safe-build's
memory and time limits, then stops the processes even if the command fails.
Only requested services get connection variables: `MONGODB_URI`, `DATABASE_URL`,
`REDIS_URL`; MongoDB also gets `MONGOMS_SYSTEM_BINARY=/usr/bin/mongod` and runtime
downloads are disabled. Tests may instead start the bundled binaries themselves,
with loopback listeners and data under `/work`.

## Qualification evidence

On the local Ubuntu 24.04 ARM64 test VM, the full image passed the offline canary
inside the native Codex sandbox with UID 1000, read-only root, all capabilities
dropped, the existing seccomp and `forge-runner-private-proc` AppArmor policies,
and `--network none`. The Mongoose, SQL and Redis round trips, browser check and
credential-isolation checks all passed. No model or provider calls were made.

The read-only probe against `XRPL-Commons/glow` at
`ceaba9628caf8c421958e1732713c375d317f49e` proposed `services: [mongodb]`, citing
`package.json`, two module manifests, `docker-compose.yml`, the end-to-end Compose
file and `tests/e2e/fixtures/global-setup.ts`. No profile was applied to a live
factory. Running glow's remediation PR remains a separate live qualification step.
