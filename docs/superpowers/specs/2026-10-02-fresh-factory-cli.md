# Fresh factory setup through the local CLI

Implementation target for [#136](https://github.com/XRPL-Commons/forge/issues/136).

## Operator experience

Install the local client using a documented one-line installer, or clone this
repository and install the client. Run `forge setup new`. The wizard walks from
a fresh VM to a verified factory and an explicit Start decision. The operator
never has to open a remote shell, transfer files, or edit server configuration.
SSH is an internal transport for installation, not an extra manual setup step.

Forge is the product. Each factory has a display name, such as Anvil, independent
of its network address. A factory can manage multiple repositories and agents.
Renaming it must not change authentication identity or repository connections.

## One deterministic workflow, two interfaces

All installation and configuration operations live in ordinary, testable code.
There is no LLM inside the CLI. The wizard collects inputs and presents results;
an external agent calls the same operations using explicit arguments and JSON.
Neither interface duplicates the server's onboarding logic.

Given the same intent and observed state, an operation chooses the same action.
Network failures and external approvals are reported as explicit states. This
does not promise byte-identical builds or deterministic model-generated code.

The surface must provide:

- `forge setup new`: interactive wizard on a terminal; JSON mode never prompts.
- Discoverable command schemas, including required inputs and result states.
- Explicit plan, apply, status, and resume operations.
- One saved setup identity, binding destination, source revision, and factory.
- Exact next actions for pending browser approvals, missing input, and errors.
- An explicit Start operation after the shared readiness gates pass.

Save the source commit selected by the initial plan. A resume must not silently
switch to a newer main branch. Record completed stages durably, observe actual
state on resume, and reject conflicting intent. Repeated setup must not duplicate
Apps, rotate credentials, revoke connections, reset a factory, or start work.

## Stages

1. Collect factory name and explicit SSH destination. Verify host identity and
   usable administrative access. Inspect OS, capacity, AppArmor, and conflicts.
2. Present the installation plan, including source commit, packages, service
   account, managed locations, services, and private network access.
3. Prepare the supported host and invoke the existing Forge installer and runner
   qualification. Run durable installation work independently of the local
   terminal; expose bounded progress and reconnect after interruption.
4. Configure private HTTPS and the authorized first operator through the shared
   sign-in bootstrap. Complete required identity-App and browser approval steps.
5. Select the GitHub owner and register/install the four distinct role Apps
   through shared onboarding. Preserve existing registrations on resume.
6. Connect coding accounts through private secret input or browser/device login.
   A caller must be able to resume a pending login without guessing shell output.
7. Select and prepare the first repository using local GitHub authority and the
   same requirements and protection policy used by repository setup.
8. Run shared host, provider, repository access, protection, and bypass checks.
9. Present readiness, obtain explicit Start intent, and observe activation.
10. Offer the factory URL and a scoped agent connection for routine operations.

Host setup authority is separate from a routine agent repository connection.
Routine repository work must not require SSH or administrator credentials.

## Credential and recovery boundaries

GitHub administrative credentials stay on the operator's computer. Only Forge's
own identity/role App credentials and required coding-provider runtime credentials
reach the server. Never place secrets in arguments, setup journals, JSON results,
agent messages, or diagnostics. No anonymous web endpoint accepts setup secrets.

Manifest challenges remain bound to authenticated intent, role, owner, expiry,
and setup generation, and are consumed once. Expired grants, stale revisions,
changed host keys, and reset generations stop the old operation.

Do not overwrite an existing unmanaged installation, policy, proxy, or service.
Report the conflict and a concrete supported recovery action. A failed or
interrupted operation must retain enough state to inspect and resume through
the CLI; documentation must not routinely send operators into a remote shell.

## Release acceptance

The full integration test ends with a real issue merged by Forge. A successful
installer, healthy services, or a passing provider probe is insufficient.

Use a dedicated disposable VM and an explicitly selected test repository.
Record its destination in private setup state, outside source control. The
existing Forge production server is outside the wipe/reinstallation test scope.
Rebuilding the VM and deleting GitHub Apps/repositories are separate actions
with separate effects.

Test both entry points, starting from a clean supported VM each time:

1. Install the CLI by the documented method on the operator computer.
2. Complete setup entirely through the wizard, or entirely through documented
   noninteractive CLI operations plus the required human browser approvals.
3. Verify the intended operator can sign in and the intended agent can connect.
4. Prepare the test repository, verify readiness, and explicitly Start.
5. Submit a small test issue with concrete, objectively checkable requirements.
6. Observe supervisor assignment, worker execution, review, security audit,
   required checks, and merge through the production workflow.
7. Verify the resulting change on the default branch, passing required checks,
   and Forge's issue completion state. Record issue/PR links and merged commit.
   Do not substitute a manual merge, bypass required checks, or weaken policy.

Recovery coverage:

- Disconnect during host preparation and while installing services; resume from
  a new local process without starting duplicate work.
- Interrupt each browser/provider handoff; reuse valid pending state and reject
  expired or reset state.
- Rerun completed setup; compare App IDs, operator sessions, client grants,
  repository policy, and activation state before and after.
- Use synthetic administrative credentials to verify they never cross into
  server requests, state, logs, or workers, including failure paths.
- Verify that missing controller bypass or another failed readiness requirement
  prevents Start through both interfaces.
- Rebuild the disposable VM between clean-install runs; explicitly verify its
  new host identity before accepting it as the replacement target.

Report unit/contract tests, fresh-host installation, and issue-to-merge evidence
separately. Leave #136 open until the full acceptance path has been demonstrated.

## Primary metric: time to first merge

With a fresh supported VM available, measure total elapsed time from starting
the documented CLI installation to the first qualifying Forge-generated PR
merged into the test repository's default branch. Include CLI installation,
setup, browser approvals, provider login, readiness checks, issue execution,
review, security audit, CI, and merge. Do not start the clock only after setup.

A qualifying merge satisfies the test issue's predeclared requirements, passes
the required checks and reviews, and is merged by the normal Forge workflow.
Manual merges and policy bypasses do not satisfy this metric.

Persist the run's start across retries and process restarts. Record these stage
timestamps to explain the total:

- CLI installed.
- Host qualified and private HTTPS available.
- Operator, role Apps, and coding account setup complete.
- Repository ready and factory started.
- First issue eligible, first worker started, and first PR opened.
- Required checks and reviews passed.
- PR merged, with GitHub evidence, and issue completion confirmed.

Report total elapsed time as the primary result. Separately report time spent
waiting for people, external services, and Forge work so slow stages can be
improved; do not subtract those waits from the primary metric. Keep failed or
abandoned runs visible alongside successful timings, including their stopping
stage. Compare repeated runs using the same VM class, source revision, provider
configuration, and test issue. Record prerequisite differences instead of hiding
them in a faster result.

Store measurements locally by default, without credentials or raw provider
output. This metric does not authorize adding remote telemetry.
