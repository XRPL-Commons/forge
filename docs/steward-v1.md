# Steward v1

The steward handles failed work, detected stalls and preparation of a retry brief. It reads an immutable dossier and returns one to four ranked choices, one recommendation and a confidence. Triage and conversation remain separate until Run 4.

## Decisions and authority

The dossier pins issue membership and requirements, the candidate, independent holds, runtime configuration and repository autonomy. It includes references to retained attempt evidence and durable repository notes. A later change makes an unselected decision stale.

The default autonomy is `reversible`. An obvious recommended retry, wait or rebuild can proceed automatically. A `manual` repository policy requires operator selection for every choice. Split, drop, fold, escalation and questions always require an authenticated operator choice. Change this policy in Settings → Repositories → Steward autonomy. The core stores policy revisions under the repository's immutable ID. A change invalidates older recommendations.

Selection records the exact stored choice. The browser sends identifiers and a request ID; it cannot alter the proposed parameters. Interrupted replies keep the choice locked until a fresh read, with an explicit retry using the same request ID.

There are at most three logical steward cycles per delivery. A separate brief consumes a cycle. Only a sealed provider failure or a proven pre-agent Forge failure can retry the same cycle, with at most three physical invocations. Review and Audit remain independent. A steward choice cannot reset failure charges, clear independent holds or bypass unsettled publication or merge work.

## Applying choices

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

Before deployment:

- Review the combined core, executor, outbox and UI change independently.
- Pass full CI and qualify the new steward schema on the installed provider image.
- Exercise the captured stuck-work cases through automatic application or one operator choice, including a grouped delivery and an interrupted remote reply.

Keep #258 and the Run 3 tracking plan open until those acceptance checks pass. Unit tests alone do not establish live acceptance.
