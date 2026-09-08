# Verified builder handoff

`python3 -m forge.handoff check --config repos.d --state state --session NAME-build`
performs read-only verification. `release` repeats that entire check, compares
evidence, and stops only the verified tmux session ID. The supervisor must hold
its existing singleton lock around release and admission.

A builder is eligible only when its enabled repository has a successful snapshot
at most 300 seconds old, no builder queue demand, and exactly one active issue.
A fresh GitHub PR read must prove exactly one published, non-draft PR by the
configured author closes that issue, has the exact local HEAD, and is ready for
review or audit without hold or changes-requested labels. The clone's origin and
root must match configuration; tracked and untracked changes prevent release.

The session must contain one pane, with Claude in the foreground at an explicit
empty input prompt and idle footer. The pane process tree must contain only bash
and Claude: background jobs, tool processes, additional panes, uncertain terminal
state, and errors prevent reclamation. Process and tmux generation identities
must match across the two checks. Local snapshot, HEAD, and worktree are rechecked
after each network read. PR reads are bounded to ten candidates and fifteen
seconds per command. The implementation is conservative: MCP subprocesses also
prevent release even when they might be idle.

Successful release removes only the supervisor's `.up` marker and records
`state/handoffs/NAME-build.json` plus an activity `session_stopped` event with reason
`verified_handoff`. Claims, PRs, files, branches, and labels remain intact. A
subsequent changes-requested PR creates builder demand and can wake a new session.
A critical PR waiting for human approval remains subject to that approval gate;
releasing a finished builder does not authorize merging it.

The checks narrow the gap between observation and teardown; they are not atomic
with arbitrary external user/process actions. The singleton prevents another
supervisor from dispatching concurrently. This is not a generic idle-session
killer and does not infer abandonment from age or silence.

## Completed work releases its slot

A completed builder may remain on the published PR head or return to a clean
main checkout. The latter requires local HEAD, origin/main, and the freshly
queried PR base to agree. Both PR head and base are compared across verification
passes. A completed reviewer can be released when its review queue is empty and
the linked PR has moved to audit, with a clean base checkout and all the same
idle/process/generation safeguards.

Claude's faint prompt suggestions are not unsent operator input. The verifier
preserves terminal styling and accepts a suggestion only if every non-whitespace
character after the prompt is faint. Typed or mixed-style input prevents release.

`forge.human_wait` handles completed critical audits awaiting human approval.
The supervisor rechecks live ownership, all audit candidates, exact head/base,
labels, reviews, and candidate-bound audit evidence before suppressing retries.
A new approval, requested change, candidate update, or any uncertainty returns
the repository to normal auditing. No gate result authorizes a merge.

A verified idle auditor can be retired while its issue remains held at audit.
Release requires the inherited supervisor singleton lock, a clean configured
clone, no child work, and two matching live checks. Receipts are stored in
`state/human-waits/` and the activity ledger. Human approval therefore remains an
operator decision without consuming an execution slot or repeated model calls.

The supervisor waits sixty seconds between reconciliation passes (configurable
with `SUPERVISOR_POLL`), rather than four minutes. This removes an avoidable
handoff delay without increasing concurrent-worker or memory limits. A complete
pass still takes time to fetch fresh repository state and launch eligible work.

After a PR merges, its active claim and stage queue entries may disappear. Empty
review and audit roles therefore also have a completion path independent of those
entries: both fresh local and GitHub role queues must be empty, the clone must be
clean on main, and GitHub must prove its exact HEAD is ancestral to current main.
This path preserves the existing repeated idle/process/session checks and does
not apply to builders with unresolved work.
