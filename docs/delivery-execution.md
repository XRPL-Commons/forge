# Approved delivery execution

SQLite owns a delivery's membership, dependencies, attempts, leases, failure
budget and exact candidate. Repository planning proposes compositions; an
operator approves each composition before `Delivery.compose_in` creates it in
the same transaction as proposal acceptance.

## Composition

There is no issue-count limit on a composition. Stored approval and worker plan
byte limits still apply. Approval hashes pin every issue version, requirements revision, source unit
version and member identity, the internal dependency graph, the grouping reason,
and independently verified unmerged observations for every existing PR.
Composition preserves source histories and carries their current failure charges
into the new unit through references to the original attempts.

A composition with existing PRs must explicitly select either a new PR or one
existing publication target. Continuing an existing PR pins its unit, candidate,
PR number, head and author identity, and requires `ownership_transfer: true`.
The operator's choice authorizes that target, including a human-authored PR;
it does not change its author. Every approved source candidate remains an exact
builder input, with its original author identity pinned in the applied approval.
Exact approved non-target PRs permit read admission while they remain at that
head; this grants no publication rights. Changed heads or authors require a new
composition decision. Composition never closes or supersedes another GitHub PR.

## One builder, separate executions

`unit_builders` records stable logical ownership. Actual processes still use
ordinary jobs, attempts and leases:

1. `reserve_builder(stage='planning')` reserves one unit-wide execution. Its
   input includes exact member pins, dependency graph, approved existing heads,
   publication target, accepted outputs and remaining members.
2. `save_plan` validates a bounded plan containing exactly one child for every
   remaining member. Each child has a stable plan key, scope and the exact
   approved direct prerequisite members. A malformed worker plan raises
   `InvalidDelegationPlan`; its controller records one genuine failed attempt.
3. Cleanup releases the planning process's provider and capacity leases. The
   logical builder remains the owner while children run.
4. `delegate` reserves an ordinary member-scoped job in a unique isolated
   worktree. Independent members can run concurrently. Each child pins the
   exact accepted prerequisite outputs, including commit and manifest hashes.
5. `accept_part` requires verified commit/base evidence and a retained captured
   worktree bundle. Bundle chunk continuity, total size and capture digest are
   checked in the transaction; the controller verifies Git objects and ancestry
   before invoking the command. Children cannot publish candidates.
6. Once every current output is accepted and every child is cleaned,
   `reserve_builder(stage='integration')` starts another execution under the
   same builder identity. Its manifest pins all accepted outputs. Only this
   execution can publish the combined candidate to the approved target.
7. Existing unit-wide Review and Audit each assess that exact candidate once.
   Late-publication recovery may adopt an independently verified publication
   only from an integration assignment whose full input is still current.

`DeliveryQueries.assignments` is the scheduler's shared source for planning,
ready children, integration or a pending reason. Its `allowed_members` input
filters freshly eligible child IDs before the bounded result page is selected. `tree` supplies bounded job
history using a job-ID cursor. `job_members` is the uniform immutable scope for
all jobs, including Review, Audit and Recovery; migration 22 preserves original
launch requirement pins and retires the scalar launch column.

## Invalidation, retries and cancellation

An accepted output is current only while its requirements, base, policy and
exact predecessor outputs remain current. An explicit member drop invalidates
that member and its descendants, preserves unrelated outputs and shared charges,
and requires each direct dependent to explicitly wait on the released issue as
an external prerequisite. The drop command requires quiescent workers and
completed mandatory investigations. A batch reduced to one member retains its
approved builder and publication authority; `delivery_approved` identifies that
authority independently of current member count. Closed members require a separate closure
decision. Failed child retries use a new planning assignment; a plan key never
silently creates another child execution.

Scoped fencing revokes the affected member, its descendants and any unit-wide
execution while preserving independent child authority. Fencing never releases
leases: verified cleanup does. Every genuine failed assignment may charge the
shared budget once; cancellation and failure propagation add no charge. The
third charge fences the tree. Operator retry explicitly starts a new budget
epoch after required investigations.

The store performs no provider, filesystem or GitHub operations. External
publication and bundle verification remain controller responsibilities, bound to
the exact stored assignment. Native provider delegation is usable only when the
execution adapter can track these same identities and outputs.

## Managed provider execution

Codex, Claude and Grok declare managed child support. Their untracked native
subagent tools stay disabled: each planned child uses an ordinary tracked job,
provider lease and disposable checkout. Planning and children receive GitHub
read credentials only. The integration execution is the sole publisher. Runtime
disable prevents new launches and fences reserved work; cleanup still releases
its resources.

The controller retains child Git bundles as immutable chunks with a capture
digest. It independently validates their claimed head, assigned ancestors and
bounded commit graph before accepting outputs. Bundles include the complete
commit history since the assigned base so this independent check proves every
required prerequisite is an ancestor, without trusting the worker report. A bundle is limited to 16 MiB;
validation has one 15-second deadline and a 10,000-commit limit. Dependent
checkouts receive the exact transitive bundle closure in dependency order, one
bundle at a time, and Git verifies connectivity in the disposable repository.
No shared writable worktree or live child credentials cross assignments.

Integration fetches all approved existing heads and accepted output commits.
Merge conflicts remain in its checkout for the owning builder to resolve. The
controller verifies that the published head contains every required input and
that its version-2 submission marker names the exact unit, members and attempt;
its body must also close every member issue. An existing target is checked again
inside the runner before the provider starts, including its approved author and
previous head. For updates, the builder marks the PR draft, records its exact
final-head marker before pushing that commit, then marks it ready last. There is
no null-head exemption from ownership checks. A changed target returns an operator decision and preserves the
existing work. GitHub publication is an external effect; exact late-publication
recovery handles a completed push whose original execution did not return.

## Operator progress

Issue Details offers a lazy **Combined delivery** section. It shows the shared
failure budget, every current member, accepted outputs and parent/child runs.
Output remains a separate authenticated read. A shared run lists its immutable
members explicitly; a child is shown only on its assigned issue. Invalidating a
review candidate preserves the last selected publication as the builder's
continuation target, so rework updates the same PR.

Agents can use `forge repo delivery OWNER/REPO NUMBER --server HTTPS_ORIGIN --json`, then pass `--after` with `next_cursor` for another page. This uses the
existing `repositories:read` grant and exposes no worker credentials, prompts or
raw output.

## Change membership explicitly

Ordinary pause, backlog, environment and runner-reset actions target the selected
member and affected descendants. A shared owner execution is fenced if its input
scope changes. Whole-delivery operations have their own preview and approval;
removing a member does not silently close the group or its PR.

The issue panel's **Grouped delivery** controls preview **Remove from delivery**,
**Close this issue** (or **Remove closed issue**) and **Dissolve delivery**. The
preview pins membership generation, the affected members and every direct
dependent's explicit choice to wait on the removed issue as an external
prerequisite. The durable request waits for verified cleanup, mandatory
investigations and confirmation that the current PR has not merged.

External agents use the same preview:

```sh
forge delivery inspect OWNER/REPO ISSUE_KEY --server https://forge.example --json > decision.json
forge delivery drop OWNER/REPO ISSUE_KEY --server https://forge.example --decision decision.json --reason "Split this scope into separate work" --apply --json
```

`ISSUE_KEY` is the exact `key` returned by `forge repo issues`; it identifies both
the repository and issue. Inspect `decision.json` before applying it. The CLI
submits that exact revision and does not refresh the approval silently. Use
`close-member` or `dissolve` in place of `drop` for those decisions, and reuse a
canonical UUID through `--request-id` when retrying the same request.

Inspection requires `repositories:read`; membership changes require the separate
`repositories:triage` connection grant. Closing a member closes only that selected
GitHub issue, never the shared PR. Dropping preserves shared charges and the
released issue's inherited history. Dissolution is an explicit decision to create
singleton budgets; it retains the old delivery history and cannot erase an
uncertain merge or release an uncleaned worker lease.


Stopping a shared builder, Review or Audit run creates a whole-delivery pause.
Use **Resume delivery** or `forge delivery resume-delivery` with the same inspected
file and explicit approval to clear that pause after cleanup. This preserves each
member's own pause, backlog, label hold, environment gate, accepted output and
failure history. Resuming one issue cannot silently resume the whole delivery.
