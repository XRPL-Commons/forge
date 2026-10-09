# Approved delivery execution

SQLite owns a delivery's membership, dependencies, attempts, leases, failure
budget and exact candidate. Repository planning proposes compositions; an
operator approves each composition before `Delivery.compose_in` creates it in
the same transaction as proposal acceptance.

## Composition

Approval hashes pin every issue version, requirements revision, source unit
version and member identity, the internal dependency graph, the grouping reason,
and independently verified unmerged observations for every existing PR.
Composition preserves source histories and carries their current failure charges
into the new unit through references to the original attempts.

A composition with existing PRs must explicitly select either a new PR or one
existing publication target. Continuing an existing PR pins its unit, candidate,
PR number, head and author identity, and requires `ownership_transfer: true`.
The operator's choice authorizes that target, including a human-authored PR;
it does not change its author. Every approved source candidate remains an exact
builder input. Composition never closes or supersedes another GitHub PR.

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
ready children, integration or a pending reason. `tree` supplies bounded job
history using a job-ID cursor. `job_members` is the uniform immutable scope for
all jobs, including Review, Audit and Recovery; migration 22 preserves original
launch requirement pins and retires the scalar launch column.

## Invalidation, retries and cancellation

An accepted output is current only while its requirements, base, policy and
exact predecessor outputs remain current. An explicit member drop invalidates
that member and its descendants, preserves unrelated outputs and shared charges,
and requires each direct dependent to explicitly wait on the released issue as
an external prerequisite. The drop command requires quiescent workers and
completed mandatory investigations. Closed members require a separate closure
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
