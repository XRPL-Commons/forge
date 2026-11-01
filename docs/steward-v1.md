# Steward v1

The steward handles failed work, detected stalls, retry briefs, repository planning and issue conversations. Every mode reads an immutable dossier v1 and returns decision v1: one to four ranked choices, one recommendation, a confidence and an optional choice-specific brief. Repository planning returns a bounded collection of these decisions, one per proposed row.

## Subjects and captured authority

A dossier identifies the existing object whose authority governs the decision:

| Subject | Exact identity | Application authority |
| --- | --- | --- |
| `unit` | `unit_id`, plus the dossier's membership, requirements, candidate and gate pins | Existing delivery selection and application receipts |
| `proposal_row` | `proposal_id`, `row_id`, captured `version` and context digest | Versioned row edits and explicit proposal acceptance |
| `conversation_turn` | `thread_id`, captured `version`, latest operator `message_id` and authenticated `actor_id` | Existing message-bound operator request and undo receipts |

Each decision echoes its dossier ID and digest. Captured subject decisions retain their physical execution identity, so later captures do not replace their history. Planning and conversations keep their existing leases and storage domains; neither creates a delivery merely to run the steward. Historical triage and discussion captures still complete through their original contracts.

## Delivery decisions and authority

The dossier pins issue membership and requirements, the candidate, independent holds, runtime configuration and repository autonomy. It includes references to retained attempt evidence and durable repository notes. A later change makes an unselected decision stale.

The default autonomy is `reversible`. An obvious recommended retry, wait or rebuild can proceed automatically. A `manual` repository policy requires operator selection for every choice. Split, drop, fold, escalation and questions always require an authenticated operator choice. Change this policy in Settings → Repositories → repository → Automation → Recovery. The core stores policy revisions under the repository's immutable ID. A change invalidates older recommendations.

Selection records the exact stored choice. The browser sends identifiers and a request ID; it cannot alter the proposed parameters. Interrupted replies keep the choice locked until a fresh read, with an explicit retry using the same request ID.

There are at most three logical steward cycles per delivery. A separate brief consumes a cycle. Only a sealed provider failure or a proven pre-agent Forge failure can retry the same cycle, with at most three physical invocations. Review and Audit remain independent. A steward choice cannot reset failure charges, clear independent holds or bypass unsettled publication or merge work.

## Planning and conversation choices

Repository rows offer ranked alternatives with identical issue membership. **Choose this option** updates the row; **Accept** separately approves it. Partial acceptance, rejection and explicit approval of each group remain unchanged, including groups with existing PRs. A manual row edit clears its association with a captured choice. A matching accepted planning brief may select a configured environment through the existing environment policy and label effects; independent operator selections remain authoritative. See [Repository planning](repository-planning.md).

Conversations use the same ranked-choice card. An `answer` choice cannot submit a mutation. Selecting another offered choice submits its stored identity through the normal operator confirmation path. The exact close-command grammar remains an authenticated operator instruction; worker wording cannot grant it authority. Undo still requires a confirmed reversible operator effect. See [Issue conversations](issue-conversations-reference.md).

Each proposal and each authenticated conversation message permits at most three physical worker executions, including failed attempts. A proposal at the limit shows `steward_cycle_limit`; use **Triage now** to request a fresh proposal. A conversation at the limit records an explanation; send a new question or instruction to continue. Repeating the same unanswered message or replaying the same request does not reset the limit. These executions do not charge a delivery's failure budget. This subject limit is separate from the delivery's three logical steward cycles described above.

## Applying delivery choices

- **Retry:** freezes the adjustment and brief into the next matching assignment. A task-specific choice authorizes one new execution of that exact task; unrelated tasks retain their original settings.
- **Wait:** records verified issue prerequisites through the existing dependency machinery.
- **Rebuild:** invalidates the old candidate through the normal lifecycle and pins the observed base.
- **Split:** creates children, records the preserved intent on every source issue, then closes the source issues.
- **Drop:** records the intent on every source issue before closing it.
- **Fold:** records the intent on the destination and every source before closing the sources.
- **Question:** posts the question while keeping the original obligation open.
- **Escalation:** requires an existing trusted fault signature. The UI offers the existing prepared Forge report for that signature. The choice remains an operator-owned wait until a person files or resolves the report; preparing a draft never claims it was published.

GitHub mutations use the existing external-effects outbox. Each step has an immutable identity, retry state and confirmation. A lost POST reply is reconciled by an exact bot-authored marker and payload. If the outcome remains unknown, Forge waits instead of repeating the POST. Comments and child creation precede closures. New writes check current local authority, remote issue state, runtime enablement and repository admission.

The UI displays the complete original issue scope. A recorded choice stays pending until all required remote actions are confirmed and the local transition completes. An issue closure does not claim its pull request was merged.

## Release checks

Before production rollout:

- Review the combined core, executor, outbox and UI change independently.
- Pass full CI and qualify the new steward schema on the installed provider image.
- Exercise the captured stuck-work cases through automatic application or one operator choice, including a grouped delivery and an interrupted remote reply.

Qualify a candidate on an idle test instance with automation disabled. Install its exact commit through `forge migrate upgrade`, then use `forge setup qualify-roles --role steward` for the provider check. Upgrades are forward-only: when installing a PR head, merge that PR with a merge commit so the release retains the tested commit in its ancestry. This provider check uses disposable work and does not establish live choice or issue-to-merge acceptance.

Keep #258 and the Run 3 tracking plan open until those acceptance checks pass. Unit tests alone do not establish live acceptance.
