# Implementation status

`forge/factory` is Forge's supported operating mode. It uses a durable supervisor,
SQLite state and disposable Linux workers. The tmux supervisor and operational
JSON stores are retired. See [runtime configuration](factory-runtime-reference.md).

## Installation and operation

- `forge install` saves the approved plan, checks prerequisites, installs the host
  and creates one GitHub App. Host preparation still precedes some browser consent.
- The CLI hands work to the web app after the server confirms ownership. Web
  progress shows preparation, account actions and the first-merge test. Codex
  browser sign-in and recovery are available there; other account modes use the CLI.
- Repository selection and activation are explicit. Per-repository settings cover
  merge approval, review scope, planning and execution requirements.
- GitHub branch protections are observed, not created by Forge. A paid GitHub plan
  is not a general prerequisite. Existing repository rules can still block a merge.
- Reset uses durable operations, verified teardown and generation checks. It
  preserves lifecycle and audit history.

## Execution and publication

- The supervisor reserves assignments before launch, checks ownership and fences
  stale work. Cleanup must be verified before resources are reused.
- Jobs use disposable containers with native tool policy, seccomp/AppArmor,
  resource limits and an internal network. Containers share the Linux host kernel;
  no VM isolation or macOS worker is claimed by this implementation.
- Source and private Git dependencies come through the host's read-only mirror.
  Jobs hold no GitHub App keys or installation tokens. Mirror policy combines
  declared dependencies, permitted organization installations, live access and
  per-project deny entries.
- Workers return artifacts. A host publisher validates them against the reserved
  assignment and authorized target, then performs revision-checked GitHub writes.
  Publication signing requires a qualification receipt for the current context;
  workflow edits require explicit permission.
- Independent Review and Audit produce revision-bound evidence. The supervisor
  checks that evidence, applicable human approval and existing GitHub rules before
  its own merge. Human-authored PR reviews also have visible work records.
- New installations use one App for publication, checks and GitHub sign-in fallback.
  Existing role-App identities remain recognized for compatibility and history.
- Claude API-key execution has a host provider proxy with job-scoped capabilities.
  Other providers/login modes still supply provider authentication to the harness
  and rely on the native tool sandbox. Do not generalize the GitHub credential
  boundary to all provider credentials; see [Security](../SECURITY.md).

## Repository planning and combined delivery

- Revisioned repository notes, rules and proposals support manual, scheduled and
  event-triggered triage. Acceptance checks current membership, prerequisites,
  source PRs and operator decisions before applying durable effects.
- An approved group has one logical builder and one candidate PR. Tracked child
  tasks use isolated checkouts; the owner integrates their results before Review
  and Audit. Execution tasks and approved issue membership remain distinct.
- Member-specific holds, execution requirements, failure budgets and cleanup
  survive rework. Closing one issue does not silently complete a grouped delivery.
- Activity and issue details expose progress, decisions and retained history.

## Persistence

One selected SQLite store holds lifecycle, configuration, operator identity and
supporting state. WAL with FULL durability, transactions, revisions and constraints
protect local state. Managed launchers qualify the runtime and reject incompatible
state. Long-lived credentials remain private referenced files. The earlier JSON
migration was one-way; its archived input is not a current backup.

Do not run independent controller roots against the same repositories. Back up
SQLite consistently with its instance metadata and credential references. The
supervisor prunes eligible raw logs after 30 days while retaining events,
structured results and active/pinned evidence. See [schema upgrades](store-migration.md).

## What remains to prove

Implemented behavior is distinct from release qualification. Synthetic lifecycle,
restart and boundary tests and real sandbox canaries provide specific evidence;
they do not prove an authenticated production workflow for every deployment.
