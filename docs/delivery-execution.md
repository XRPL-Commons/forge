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

## One builder, separate execution tasks

An approved delivery defines which issues one PR must resolve. Its builder
chooses how to do that work. A task may cover several issues, several tasks may
contribute to one issue, and the builder may implement work itself. Task choices
stay within the approved members, policy, failure budget and publication target;
they require no new human approval.

`unit_builders` records stable logical ownership. Every physical execution uses
ordinary jobs, attempts, isolated worktrees and leases:

1. A planning execution receives every exact member requirement, approved issue
   dependency, existing PR head, publication target and reusable task output.
   It returns a bounded version-2 plan with tasks, task dependencies and final
   owner work. It cannot publish code.
2. Each task records its own ID, scope, `owner` or `child` executor, member
   requirement pins and prerequisite tasks. Member scope is many-to-many;
   `job_members` records the exact scope of every physical assignment.
3. Planning releases its process and capacity. Ready tasks run within existing
   provider, environment and capacity policies. Independent tasks can run
   together even when they contribute to the same issue. An owner task can
   produce work needed by a later child without holding a coordinator process
   idle.
4. Each task receives the exact current prerequisite outputs. Its accepted
   output requires independently verified Git ancestry and a retained captured
   bundle. Owner and child tasks have read-only publication credentials.
5. Final integration waits for every planned output and verified execution
   cleanup. The same logical builder receives each unique output, performs any
   remaining owner work, resolves conflicts, and verifies every issue's
   acceptance criteria. Only integration publishes the one approved PR.
6. Existing Review and Audit each assess that exact candidate. A task's request
   for human verification remains attached to its output and must reach the
   final candidate's verification gates.

The plan can have no tasks if final owner work covers the whole delivery. Work
needed by a child must be an earlier owner or child task; it cannot be postponed
until integration. Replanning requires quiescent executions and explicitly
retains still-current task definitions and output closure. Reusing a task label
alone never reuses its authority or result.

### Issue dependencies and task dependencies

Approved issue dependencies remain authoritative. For a prerequisite issue with
two planned contributions, a dependent task consumes both exact outputs. The
builder may add task dependencies. A task spanning both ends of an approved
issue dependency must explicitly internalize that ordering in its scope.
Forge compiles the two graphs and rejects cycles or ambiguous internalization.
External prerequisites, holds, ownership and environment requirements still
apply to every issue in the task.

An issue's contribution set is ready only when all its planned contributions are
current. Accepted integration evidence establishes any remaining owner work.
This is progress toward the candidate; issue completion still follows Review,
Audit, required approvals and normal merge. A single successful task never
stands in for the whole issue.

## Sharing factory capacity

Dispatch keeps a bounded traversal cursor so blocked deliveries cannot hide later
work. Within each page, Audit precedes Review, then build work. Deliveries in the
same role get turns in least-recent allocation order, with unserved deliveries
first and unit ID breaking ties. Planning, each child and integration all consume
one build turn; a large group does not retain the next free slot automatically.

Allocation order comes from the retained `attempt_reserved` event sequence joined
to its exact job role and committed launch. Reservation, leases and that event
commit together. A restart keeps the order; blocked visits, failed transactions
and command replay do not create another turn. Migration 29 adds an index for
these lookups without changing stored work or allocation history.

The existing memory escalation drain takes precedence over ordinary turns so
smaller work cannot continually refill capacity needed by an eligible larger
retry. Provider availability, capacity leases, fresh admission and scoped fences
are still checked before a reservation succeeds.

## Invalidation, retries and cancellation

A task output is current only while all its pinned member requirements, base,
policy and exact predecessor outputs remain current. Changing one member of a
shared task invalidates that entire output and its dependent task outputs.
Disjoint unaffected tasks retain their own authority and output. An explicit member drop invalidates
that member and its descendants, preserves unrelated outputs and shared charges,
and requires each direct dependent to explicitly wait on the released issue as
an external prerequisite. The drop command requires quiescent workers and
completed mandatory investigations. A batch reduced to one member retains its
approved builder and publication authority; `delivery_approved` identifies that
authority independently of current member count. Closed members require a separate closure
decision. Failed task retries return to owner planning after the existing investigation
and cleanup requirements. A task key never silently creates another attempt.

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

Issue Details offers a lazy **Combined delivery** section. It shows approved
members and canonical blockers separately from the builder's task plan. Tasks
show scope, executor, dependencies and current output; runs show the exact
member scope, attempt result and measured resource evidence. Shared jobs count
once toward factory capacity, even when they appear on several issue pages.
Logs remain a separate authenticated read.

Agents use:

```sh
forge repo delivery OWNER/REPO NUMBER --server HTTPS_ORIGIN --json
```

The version-2 response contains:

- `members[].contributions`: the complete task ID and exact output set for that
  member, `ready`, `owner_final_pending`, and plan/candidate identity.
- `members[].dependencies[].contributions`: the same complete prerequisite set;
  no arbitrary representative output.
- `plan` and `tasks`: current task definitions and status. `task_next_cursor`
  continues task paging through `--task-after`. The cursor pins its plan;
  `delivery_plan_changed` means restart from the first task page.
- `jobs` and `next_cursor`: physical execution history, paged independently
  through `--after`. Task runs identify `task_id`, `task_key`, `task_executor`
  and every assigned member.

Member contribution sets do not change with either history page. A task's
prerequisite IDs can refer to another task page. `tasks_complete` describes
whether the current page reaches the end; it does not mean the work is complete.
The UI returns to the first task page when the builder replaces its plan.

The existing `repositories:read` grant covers these reads. They expose no
worker credentials, prompts or raw output. Accepted output is separate progress:
it never hides a hold or another independent blocker. See
[measured execution resources](execution-resources.md) for sample timing and
availability limits.

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


Stopping a task pauses its explicitly assigned issue members and fences affected
tasks and their dependents. Independent tasks keep running. Stopping a planning,
final integration, Review or Audit run creates a whole-delivery pause.
Use **Resume delivery** or `forge delivery resume-delivery` with the same inspected
file and explicit approval to clear that pause after cleanup. This preserves each
member's own pause, backlog, label hold, environment gate, accepted output and
failure history. Resuming one issue cannot silently resume the whole delivery.

### A target branch moves during a build

A verified publication must still match the base pinned by its build assignment.
If the PR's base has advanced before result acceptance, Forge revokes that exact
cleaned capture. The normal stale-result transition releases its result wait
without a failure charge or investigation; other operator and environment gates
remain in force. The next assignment reads the fresh base.

For a combined delivery, the revocation receipt retains the verified PR number,
branch, head, repository, target branch, and author. This is continuation authority,
not an accepted review candidate. Subsequent planning and integration reuse that
PR only while its recorded identity still matches GitHub. An operator-approved
existing PR keeps its original author and publication target. Review and audit
still run against the candidate eventually accepted on the fresh base.
