# Current architecture target and delivery order

Decision: 13 October 2026, [#259](https://github.com/XRPL-Commons/forge/issues/259).
This document distinguishes the agreed target from the implementation on `main`
at `06c6351`. It supplements dated decisions; it does not rewrite their evidence.

## Agreed target

- **One Forge GitHub App.** The supervisor holds its private key and owns GitHub
  writes: commits, refs, PRs, comments, labels, checks and merges. The intended
  required permissions are Metadata read and Contents, Pull requests, Issues and
  Checks write. Workflows write is optional and subject to repository policy;
  Administration read is conditional on classic protection; Deployments read is
  optional. Commit-status permission remains an open proof, not a granted scope.
- **Workers receive no GitHub credentials.** A supervisor-owned bare mirror supplies
  repository inputs. Captured notes supply issue facts, comments, PR feedback and
  CI evidence. The image-owned harness exports a bundle and a typed result. The
  supervisor verifies and publishes the result. GitHub CLI tools and GitHub
  network destinations leave the sandbox once every worker role can operate this way.
- **Tailscale operator identity by default**, with Login with GitHub through the
  same Forge App as a fallback. An explicit operator identity and authorization
  replace the mode that treats network reachability as operator authority.
- **Independent logical Build, Review and Audit remain.** One App does not combine
  their assignments or let a builder approve its own result. Typed evidence,
  exact candidate pins, scoped gates and the shared failure budget remain binding.

This target supersedes the four-App registration goal and separate identity-App
wizard in [#136](https://github.com/XRPL-Commons/forge/issues/136) and
[#12](https://github.com/XRPL-Commons/forge/issues/12). Existing Apps, sessions,
installation journals and completed qualifications remain migration inputs.
Do not uninstall Apps, alias connections or discard identity state merely because
this target is documented. Current operational instructions still describe the
installed four-App system until its replacement ships.

## Current boundary

The current supervisor can mint narrowed tokens, and tracked child/planning work
already uses read credentials. Ordinary publishing builds and integration still
have GitHub write authority. Worker bundles, exact task outputs, durable captures,
scoped cleanup and supervisor-owned check effects are useful foundations; the
supervisor does not yet own every publication.

Current operator authentication uses its configured GitHub identity App and
sessions. The UI proxy does not forward Tailscale identity. Adding an unchecked
header to the allowlist is not an identity implementation.

## Delivery phases

### 1. Supervisor publication — #255

Build this around retained verified bundles and durable external effects:

1. The image-owned harness exports each ordinary build and final integration
   result, with typed PR text. Worker tokens have no write permissions. Only
   `pass` or `needs_human_verification` may yield a new candidate; a matching PR
   must not promote an `unable` or otherwise unsuccessful report.
2. Verify objects outside an executable checkout against the immutable assignment.
   Preserve modes, symlinks, deletions and submodule entries. Compute protected-path
   and removed/skipped-test facts for Review/Audit; these facts do not reject a change.
3. Journal supervisor publication intent with exact assignment, input tree,
   repository, branch and prior head. Reconcile uncertain API responses and use
   non-force head checks before retrying writes. The App creates one published
   commit per attempt and owns the submission marker.
4. Read back the published head and exact tree before candidate acceptance.
   Bind the GitHub-created commit to the verified worker bundle and task-output
   receipts. Flattening worker commits must not weaken prerequisite validation:
   original task ancestry stays proven in retained objects, and final publication
   has a separate exact-tree receipt.
5. Reuse existing candidate invalidation, Review/Audit, human verification and
   merge admission. Preserve approved existing-PR ownership transfers and exact
   previous head/author identity; migration is not permission to edit any PR by
   a former Forge bot.

**Proof required before choosing the final publisher:** in an authorized scratch
repository, establish whether a REST Git Data commit made by the installation App
without custom author/committer is verified and passes the intended signed-commit
ruleset. #255 proposes a GraphQL alternative if this fails; it would need an
explicit unsupported-mode contract rather than silently changing files. Neither
alternative is established by this document.

**Resolve #255's wording conflict:** its content section explicitly makes protected
paths reviewer facts, while its later test list says “protected-path refusal”. Use
the explicit facts-only rule. Workflow permission policy is a separate authority.
Per-file/file-count/bundle limits are transport/resource bounds; do not introduce
an arbitrary total-diff-size quality gate. Pin the actual bounds and LFS refusal
contract before implementation.

### 2. Tokenless input and output for every worker role

Add a supervisor-owned mirror and immutable job input selection. Pin repository,
base/head and exact dependency outputs; never mount supervisor credentials or a
writable shared mirror into a worker. Transfer ownership and cleanup follow the
existing job/attempt leases.

Capture bounded, attributable issue/comment/review/CI context for Build, Review,
Audit, triage, discussion and recovery. Incomplete input must be visible; do not
replace a needed fresh observation with a silently truncated or stale note.
Only after all these paths work without GitHub access remove token injection,
askpass, worker token revocation, `gh` and GitHub egress. Retain supervisor credential
lifecycle handling. Existing in-flight jobs must finish under their captured
contract or be explicitly drained before deleting the old path.

**Decision:** private Git dependencies need a mirror strategy or a declared,
repository-scoped read exception. A credential-bearing exception must be described
as an exception; it cannot count as full tokenless-worker acceptance.

### 3. One App and check-based verdicts

Collapse connection/onboarding configuration only after publication and input
boundaries no longer depend on separate role credentials. Record an explicit,
resumable mapping from existing App/installation identities to the selected Forge
App. Preserve queued effects and immutable historical assignments. Require fresh
installation and policy checks before retiring any old key or App.

Review/Audit verdicts remain independent evidence and are published as checks and
comments. GitHub review count must not be inferred from machine verdicts. The
intended baseline has the Forge App as its one pull-request bypass actor and pins
`forge/merge-policy` to its App identity.

**Open policy/proofs:**

- Does configuring the App-pinned required check need any additional permission,
  particularly Commit statuses write? Do not add it speculatively.
- Is `forge/merge-policy` required on all PRs or only the intended Forge branch
  scope? Human PRs must not acquire an unsatisfiable Forge-only check.
- Define the baseline human review count and bypass behavior together. Current
  two-review baseline wording and #259's counted-human path need one explicit
  rule; a check run does not itself count as a human approval.
- Prove signed publication and the chosen required-check/bypass behavior together,
  preserving stronger organization rules and non-Forge developers' access.

A single-App manifest replaces the four-App wizard. Keep resumable browser
handoffs, exact repository selection, local administrator credentials and explicit
Start. Reduced operator steps are a target, not measured acceptance yet.

### 4. Tailscale identity and same-App GitHub fallback

Choose and test the full trusted path: Tailscale Serve → UI proxy → status API.
A loopback bind alone does not prove the forwarded identity came from Serve; the
upstream secret authenticates the UI hop, not the browser's supplied identity.
Strip untrusted identity headers, authenticate the ingress hop, and test direct
local requests as well as normal proxied requests. Decide whether the supported
boundary uses loopback with the required process restrictions or a Unix socket.

Bind bootstrap to the explicitly selected first operator, not the first visitor.
Map identity into the existing authorization, mutation attribution, CSRF and
scoped external-agent connection model. Define revocation and sign-out behavior:
ambient Tailscale identity must not silently remove the existing operator action
that disconnects approved agents. Decide how GitHub and Tailscale identities map
to the same operator without an accidental account-linking grant.

**Proof:** same-App Login with GitHub must work for the intended operator without
an installation, with the required minimal scopes. Preserve current OAuth sessions
until a verified migration is available; deleting the old wizard is the final step.

### 5. Provider credential reduction — optional

Extend proven access-only provider credential handling where a provider supports
it. This is separate from removing GitHub credentials. Claude/Grok refresh-token
and concurrent-login behavior need their own conformance proof. Do not gate the
first four phases on this optional work or enable an unqualified provider.

## Independent work that can ship first

- Reject unsuccessful build reports before candidate promotion, with regressions
  covering ordinary and integration paths and no weakened late-capture authority.
- Preserve canonical dispatch reasons and next scheduling opportunity; expose an
  honest owner when no worker/schedule/gate explains an open unit. This is the
  first step of [#258](https://github.com/XRPL-Commons/forge/issues/258).
- Classify trusted pre-agent runner/harness failures separately from issue failures.
  Never infer a Forge fault solely from worker-authored prose. Retain one immutable
  failure identity, do not double-charge, and expose the existing evidence.
- Improve current setup resumption and specific prerequisite explanations. These
  changes remain useful during migration and must not fabricate readiness.

#258's broader steward requires a typed move menu, exact input revision, ranked
choices, one recommendation and durable decision/effect replay. Its three steward
cycles are distinct from the delivery's three-failure budget: automatic retries
must not silently reset an exhausted budget or clear independent human/hold gates.
Specify the stall interval, retry adjustments/caps and relevant-change bindings.
A new Forge release is a reason to recheck a recorded Forge fault, not permission
for an unlimited retry loop. Reporting an issue is an operator-approved external
effect; deduplicate by a safe signature without publishing private source or logs.

## Acceptance carried forward

| Existing work | What remains valid and what changes |
|---|---|
| #209 SQLite/lifecycle | Keep migration, parity, shared state/gates, ordinary merge and observation evidence. HTTP performance and combined-delivery acceptance remain separate. New publication effects must use this same authority. |
| #207/#217 delivery | Keep one owning builder, tracked task graph, exact outputs, shared budgets, one independent Review/Audit and normal member closure. The publisher moves to the supervisor; it does not change approved composition or create a new delivery ledger. Real parallel/grouped merge evidence is still required. |
| #215 planning | Keep proposals, notes, per-row approval, durable effects, dependencies and explicit merge policy. A new one-App baseline must still enforce human-required cases. |
| #136/#12 onboarding | Replace the four-App/separate identity-App target with #259. Keep deterministic CLI/wizard parity, resumability, credential isolation, truthful readiness, explicit Start, normal first merge and human-approval recovery. Preserve existing installations rather than registering replacement Apps blindly. |
| #216 qualification | Qualify coherent batches together. Keep local/mock proof separate from supported upgrade, scratch-platform proof and real issue-to-merge evidence. No issue closes merely because its architecture changes. |

The north star remains time from recorded CLI installation to the first normal
GitHub-confirmed merge, including approvals and retries. Missing installation
start remains unknown. A functioning service or a target touch-count estimate does
not establish that measurement. Optional providers and observer work stay deferred
under the existing core-completion priority.
