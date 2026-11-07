# Workers, accounts and merging

Forge starts disposable workers when a role slot, enough memory and a usable coding account are all available. A finished worker supplies evidence; the supervisor checks that evidence and GitHub again before the issue can advance or merge.

```mermaid
flowchart TD
  Q[Eligible issue] --> S{Role slot free?}
  S -->|Yes| M{Enough host memory?}
  M -->|Yes| A{Account ready?}
  A -->|Yes| W[Start isolated worker]
  S -->|No| Wait[Wait without charging a failure]
  M -->|No| Wait
  A -->|No| Wait
  W --> V[Collect result and verify evidence]
```

![A worker cube sits inside its own sandbox beside memory and account indicators; the supervisor stays outside.](assets/runtime-hero.svg)

## Workers and slots

**Build** owns implementation and the assigned pull request. One builder can
coordinate an approved group of issues, delegate independent tasks to children,
then integrate their accepted outputs. Each task has its own checkout. Builders
and children share the same role slots, memory and account limits.
**Review** checks the complete candidate, **Audit** checks security, and
**Recovery** investigates a failed attempt before the next decision.

The default role limits are 4 Build, 2 Review, 1 Audit and 1 Recovery worker. Those are ceilings, not a promise that eight workers will run: memory and accounts may admit fewer.

Each worker normally gets 4 GiB of memory and two CPUs. Forge reserves host memory before starting work and keeps 2 GiB for the host; adding slots alone does not create more memory.

Use **Settings → Worker capacity** to inspect the configured capacity. When an issue waits, read its reason rather than increasing every limit.

## Watching work

Open **Activity** to see issue work and independent pull request reviews. A PR
review appears even when Forge did not create the PR and no issue is assigned.
Open its work record to see the current commit, Review and Audit progress,
blockers, retry timing and previous attempts. Worker output loads when opened.
GitHub's Forge check links to that same record.

Queued or blocked work remains visible without a running container. If the PR
merges or closes on GitHub, Forge stops obsolete work and keeps its history.
Previous-revision results do not count as evidence for the current revision.

## What a worker can reach

```mermaid
flowchart TD
  S[Trusted supervisor: assignments, credentials, merge] --> C[Disposable worker container]
  C --> R[Assigned repository checkout]
  C --> T[Provider native tool sandbox]
  T --> F[Writable workspace and temporary files]
  T --> N[Allowed network destinations through proxy]
  T -. denied .-> H[Unassigned host files and Docker socket]
  T -. denied .-> Secrets[Controller credentials and persistent state]
```

Repository commands run inside the provider's native sandbox as well as the container boundary. Workers get their own workspace and limited network access; they do not get the host's Docker socket or the Controller credential.

Workers receive a repository checkout through Forge's read-only git mirror,
without GitHub credentials. Builders return local changes; the supervisor
validates and publishes them. Review, Audit and Recovery return evidence that
the supervisor checks before publishing role verdicts through the Forge App.
Untracked native provider delegation is disabled; the supported fallback is
Forge-managed children.

The supported runner requires Linux, Docker and the installed isolation policies.
Containers share the host kernel; they do not provide a VM boundary. If a project needs unavailable tools or a database environment, Forge reports a setup problem instead of treating it as a code defect.

For exact mounts, network rules and limits, read the [runtime reference](factory-runtime-reference.md) and the [isolation status](../SECURITY.md#isolation-status).

## Accounts and cooldowns

Coding accounts authenticate the model providers. They are separate from the GitHub Apps that access repositories, and from your operator sign-in.

Open **Settings → Connections → Coding accounts**. Codex browser sign-in can
connect or recover in the web app; other modes use the CLI. Claude API-key jobs
use a host proxy with a job-scoped capability. Other modes supply provider
authentication to the harness in the container and rely on the qualified native
tool sandbox to keep repository commands away from it. See the
[credential boundaries](../SECURITY.md#runtime-trust-boundaries).

A quota or sign-in problem can make an account unavailable. Forge shows the reason and, when known, when it can retry; waiting for a provider does not charge the issue a failed code attempt.

Connected accounts may answer discussions too. Signing out of Forge revokes external agent connections you approved; it does not uninstall the repository GitHub Apps.

## What permits a merge

```mermaid
flowchart TD
  P[Current candidate: exact head, base and requirements] --> G{Issue open, owned by Forge, repository ready?}
  G -->|Yes| CI{Required CI and verification satisfied?}
  CI -->|Yes| R{Forge Review and Audit accepted for this candidate?}
  R -->|Yes| H{Policy or Audit requires a human?}
  H -->|Yes| A{Eligible approval on this exact head?}
  H -->|No| F[Recheck protections and current evidence]
  A -->|Yes| F
  F --> M[Controller asks GitHub to merge expected head SHA]
  M --> D{GitHub accepts?}
  D -->|Yes| Done[Observe merged: Completed]
  G -->|No| Wait[Wait or ask for the missing condition]
  CI -->|No| Wait
  R -->|No| Wait
  A -->|No| Wait
  D -->|No| Wait
```

If repository access or its latest check is unavailable or stale, affected issues
show **Repository setup** with a link to restore access or recheck. Verified
readiness clears this blocker; other holds remain. New implementation work waits,
but an expired report does not cancel running workers or discard their outputs.
Planning and diagnosis can continue while repository access remains available.
Publication and merging still recheck current conditions. These checks do not
change permissions or enable automation.

A green worker result alone is not merge permission. Forge checks its App-bound Review and Audit evidence, required CI, verification, repository protections and current ownership; unresolved changes requested also prevent merging.

Merge policy is set per repository and defaults to `human`, which requires one counted approval. Explicit `auto` permits a merge after CI, Review and Audit pass; Audit or verification can still require human approval. Change it in **Settings → Repositories → repository → Automation → Merging**, then choose **Save**. A counted review must come from an eligible person with repository write access (and the configured reviewer list, if any); bots and the pull request author do not count.

The approval must follow acceptance of the current candidate and match its exact head commit. The issue panel shows the reviews found and whether each counted.

Changes to the head, base, requirements or policy invalidate earlier candidate evidence. Forge reads approvals again just before merging and sends GitHub the expected head SHA, so a new commit cannot slip through using an old approval.

Use **Save** to apply review coverage and recovery choices. **Advanced** contains dependency, environment and deployment settings; **Diagnostics** contains access and GitHub observations. [Settings reference](factory-runtime-reference.md#repository-settings) explains their behavior.

You can also set or inspect merge policy through the CLI:

```sh
forge connect https://forge.example --repo OWNER/REPO --policy
forge repo policy OWNER/REPO --server https://forge.example
forge repo policy OWNER/REPO auto --server https://forge.example
forge repo policy OWNER/REPO human --server https://forge.example
```

The connection approval explicitly includes permission to set merge policy.
Existing connections keep their previously approved permissions. The policy is
stored by GitHub repository ID, so a rename does not transfer it to another
repository. The global merge switch remains a separate prerequisite.

On upgrade, an enabled legacy automatic-merge setting is preserved only for
already configured repository IDs. Other existing repositories and all new
repositories require human approval unless explicitly configured otherwise.
Changing a repository's effective policy invalidates its previous candidate
evidence; changing another repository's policy does not.

### GitHub enforces owner rules

The supervisor verifies Forge's technical evidence and required human approval
before requesting a merge. GitHub then applies the owner's rules to that request.
Forge does not require a baseline ruleset or bypass grant. An API rejection is
reported as a merge blocker, without changing repository protections.

## Pause, cleanup and retries

Pausing prevents new work and can fence active assignments. A late worker result cannot authorize progress after its assignment was withdrawn; cleanup must still finish.

A disappeared or stopped worker is not automatically a code failure. Forge distinguishes infrastructure problems from charged failures and investigates each charged failure, including the third.

An approved delivery shares three charged failures across its builder and children.
Changing membership does not silently reset that budget. After exhaustion, read the findings and record a new approach before choosing an explicit retry. **Discuss blocker** is available for understanding the problem without spending another failure attempt.

See [How an issue moves](issue-state-machine.md) for status meanings. Technical settings, deadlines, storage retention and pre-cutover behavior are in the [runtime reference](factory-runtime-reference.md); a selected SQLite installation uses forward updates and has no JSON switch-back.

### Forge startup problems

**Forge needs a repair** means an image-owned harness failed before the agent
started. These verified import, name and syntax errors consume no issue attempts.
Forge retains the failure evidence and waits for a qualified replacement image.

The normal updater qualifies each replacement before activating it. Once every
affected worker image has changed, Forge releases this blocker and checks whether
work can resume. Other holds still apply. A configuration-only or same-image
update does not retry the fault.

This handling currently covers ordinary build, review and audit assignments.
The broader failure envelope and **Report to Forge** action are still planned.
See the [startup protocol and scope](factory-runtime-reference.md#forge-startup-problems)
for technical details.
