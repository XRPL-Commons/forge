# Verification outside the worker

## Human checks

A worker reports `needs_human_verification` with `verification.what` (steps,
inputs and expected result), `verification.why`, and `verification.checks` (empty
for a manual check). Builds must first publish a valid candidate PR. Recovery can
correct a failure that was really an unavailable acceptance check.

The request is bound to the candidate: repository, PR, head, base, requirements
and policy. Forge releases the worker without charging a failure and shows the
steps with **Approve** and **Retry attempt**. A reviewer runs the check and approves
that commit on GitHub after the request was created. A failed check should be
described on the PR, then retried with the findings. Approval resumes the remaining
review/audit work; it does not replace either technical verdict. New commits or
requirements invalidate the verification evidence. Revoked approval blocks merge.

## Repository CI: first automated verifier

Repository owners can opt in through `repository_verification` in the runtime
configuration. Each repository maps to allowed check names and their GitHub App
IDs. A worker may name these checks in `verification.checks` when they perform
the requested verification. Free-form manual requests remain human checks.

The operator installs a workflow triggered by pull requests and makes the check
required in repository protection. Forge reads check runs through the existing
controller identity; it needs no workflow write or Actions dispatch permission.
Only a single completed `success` check from the configured App at the exact head
counts. Missing, duplicate, skipped, failed or unavailable checks keep the gate
closed. Forge rechecks automated evidence before merge. The workflow should test
the PR merge revision against its base, and avoid `pull_request_target` execution
of candidate code with elevated credentials.

Example opt-in (replace the example App ID with the verified publisher):

```json
{
  "repository_verification": {
    "example/project": [{"name": "forge-verify / release-smoke", "app_id": 12345}]
  }
}
```

Use `bin/forge factory status --root <runtime-root>` to read `runtime_revision`, then:

```sh
bin/forge factory verification-configure --root <runtime-root> --target <repository-id> \
  --revision <runtime_revision> \
  --checks '[{"name":"forge-verify / release-smoke","app_id":12345}]'
```

The command refuses stale configuration. Use `--checks '[]'` to disable the opt-in.
Configure only workflows whose scope matches the requested verification. Add the
workflow through the repository's normal review process; Forge does not grant
itself workflow permissions or launch arbitrary workflow dispatches.

### Threat model

Candidate code, Dockerfiles, issue text and worker claims are untrusted. They cannot
select a host command, write workflow permissions, choose an arbitrary publisher,
or turn a skipped check into success. Operator-owned configuration authorizes the
check identity. GitHub supplies its status and commit binding. Normal protection,
review and merge checks remain mandatory. A compromised authorized CI publisher
can lie about its result; use a dedicated workflow, minimal permissions, immutable
action revisions and no production secrets for candidate tests.

## Separate image builder: later option for repositories without CI

A host verifier would run under a separate service account with rootless BuildKit
or Podman, its own AppArmor/seccomp policy, bounded CPU/memory/disk/time and an
explicit egress allowlist. The controller would enqueue an immutable repository
ID, candidate SHA and operator-approved smoke recipe. Workers would receive only
the result and bounded logs, never a Docker socket or builder credentials.

The service must fetch the exact SHA, reject mutable external build inputs unless
pinned, use disposable storage and network namespaces, and prove cleanup before
reuse. No host mounts, host networking, privileged containers, production secrets
or repository-controlled builder flags are allowed. Results must include the
candidate, recipe/config digest, image digest and exit status; cache reuse requires
all of them to match. Admission, cancellation, quotas, log redaction and fault
injection tests are prerequisites to enabling this service. It remains a separate
implementation; the first opt-in verifier uses repository CI.
