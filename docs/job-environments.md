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

## Automatic assignment per issue

The supervisor chooses an environment for each issue. No repository profile or
confirmation is required. It scans repository dependencies, test setup, Compose
and CI files in the background when assigning work. Results are cached and
refreshed after ten minutes when another issue is assigned. Each scan records the
exact repository commit and file evidence. A failed or interrupted scan does not
block dispatch; the supervisor can use earlier evidence, and the worker can report
missing databases.

Selection uses the issue title and description:

- Explicit documentation/copy-only work starts on standard workers.
- An issue naming MongoDB, PostgreSQL or Redis gets those services.
- Backend, API, integration or test work uses databases found by the cached scan.
- Otherwise the issue starts on standard workers, or an optional repository default.

The issue's **Worker environment** section shows the assignment and reason. Use
**Change environment** to choose an optional override for that issue, or return to
**Automatic**. Overrides apply to the next attempt and cannot change an active
worker. Saving an override does not queue a backlog issue or release an existing
hold. Explicit `env:standard` and `env:full` GitHub labels are also respected;
changing the selection in the UI clears those labels so the new preference wins.
Unsupported/conflicting labels or unavailable database workers defer dispatch
without charging a code failure. The controller records each assignment in history.

If a worker reports `blocked_environment`, `blocker: services`, with supported
`required_services`, the supervisor adds those databases and retries once on the
same branch. This also works when a database worker needs another service. No
repository confirmation is required. The retry is bounded to the installed
MongoDB, PostgreSQL and Redis services. Explicit overrides prevent automatic
adjustments. A repeated blocker stays held for repair and **Recheck readiness**;
neither case consumes a code-failure attempt. Older service holds can use this
same one-retry policy after cleanup completes.

## Repository evidence and optional defaults

Open **Settings → GitHub access → repository → Test databases** to inspect the
cached evidence or select **Scan again**. These scans inform issue assignment
immediately. They do not require approval.

**Optional repository default** lets an operator deliberately choose a fallback
for issues without a specific requirement. Previously confirmed profiles become
scan evidence; they no longer force a database worker on every issue.
**Use automatic selection** removes the default while preserving scan evidence.
Issue-specific choices take precedence. Database defaults require a qualified
full environment on the host.

The terminal commands remain available for administrators:

```sh
bin/forge factory environment-probe --root /path/to/forge-runtime --target REPOSITORY_ID
bin/forge factory environment-confirm --root /path/to/forge-runtime \
  --target REPOSITORY_ID --revision REVISION_FROM_PROBE
```

The first command refreshes evidence. The second saves an optional repository
default. Neither executes repository code. The probe is bounded to 60 files of
64 KiB each. An incomplete or oversized scan is reported; it is not treated as
proof that no databases are required.

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
