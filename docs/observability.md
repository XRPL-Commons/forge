# Local observability

The dashboard reads `state/observability.sqlite3`; it does not query GitHub during page loads. Collect with:

```
python3 -m forge.observability --state ./state --config ~/ops/forge-watchdog/repos.d
```

Alternatively repeat `--repo owner/name`. Collection calls `gh api` using GET only, with pagination and subprocess timeouts. No model generates summaries: whitespace-normalized body excerpts are capped at 600 characters. GitHub remains authoritative for raw issue, PR, comment and review content. The local database retains raw collected records, URLs, and summaries so details remain inspectable if a later read fails. Treat it as private repository data; the service uses umask 0077. Authenticate `gh` as the account running the service. The supplied user service/timer are templates, not installed or enabled automatically; adjust absolute paths for deployment.

First collection includes every currently open issue/PR plus issues/PRs updated in the last 30 days (`--days` changes initial coverage). Subsequent polls overlap the successful timestamp by five minutes and refresh the open inventory. A 90-day chart cannot establish completeness beyond the displayed coverage date. To explicitly backfill a longer initial window, collect into a new state directory using `--days 90`; do not delete the existing database and its observed history. Old records are retained; absence from an incremental response is never treated as deletion or completion.

Each repository snapshot commits before evidence enrichment. At most 8 changed items per repository are enriched per run (`--evidence-limit`), with up to four paginated REST endpoint calls plus one bounded `gh pr view` summary read per PR. Pending evidence resumes on later runs even when the issue update timestamp is unchanged. Open PR evidence is also refreshed after ten minutes independently of issue update timestamps. Partial status means evidence remains outstanding; repository read errors preserve the prior successful snapshot, cursor and content. An evidence error retains previous evidence and retries later. A very large endpoint can still require multiple pages: the item limit limits enrichment breadth, not the total number of HTTP pages. A local flock rejects concurrent timer or CLI collectors. Missing evidence is collected before refreshes; within that backlog, open PRs precede closed PRs and issues. Refreshes run in order of the oldest successful collection so busy repositories cannot starve older items. Overdue PR refreshes are included in pending-evidence counts. Each gh process has a 90-second timeout; GitHub errors, including rate limits, leave remaining work pending for a later run. The service has a 30-minute overall timeout.

## Interpreting the data

Stages are pending, build, review, audit, held, completed, closed and unknown. Hold overrides open stages; conflicting role labels produce unknown. Wip and changes-requested mean build. Open issues without role labels are pending. Unstaged PRs are unknown. Closed issues count as completed only when GitHub state_reason is completed. Closed PRs count as completed only with a recorded merged_at. A merged PR never closes its linked issues in this database. Counts include both issues and PRs, so they are inventory counts, not unique work-item throughput.

Daily charts count issue creation, completed issue closure, and PR merge dates reported by GitHub. They are backfilled lifecycle facts within coverage, not reconstructed stage history. Events record the first locally observed stage and later changes; the first observation has previous_stage null. A stage transition happened sometime between observations, not necessarily at the observation timestamp. Intermediate changes between polls cannot be recovered. Closed PR stubs remain unknown until merge evidence arrives. Same-poll enrichment refines the existing observation instead of emitting a spurious closed-to-completed transition; only GitHub merged_at establishes merge time.

Audit evidence is a comment or review containing an explicit Criticality: NORMAL, CRITICAL or UNKNOWN line (including Markdown), a Security audit heading, or a Criticality classification heading. This identifies audit-related text only: it does not certify audit passage, human approval, current-head validity or branch-protection compliance. Review states and commit IDs are retained as evidence. A read-only `gh pr view` query supplies GitHub reviewDecision, mergeStateStatus and statusCheckRollup with a 45-second timeout. These aggregate facts are bound to headRefOid and accepted only when it matches the collected REST PR head. They do not establish Forge-specific human authorization. Check counts distinguish failed, pending, passed, skipped/neutral and unknown outcomes; zero checks does not establish CI success. A summary failure or head mismatch leaves review_decision UNKNOWN and checks null without discarding collected review/comment/audit evidence; summary_status and summary_error explain that gap. Raw summary content, head and observation timestamp remain locally inspectable. Closing-keyword issue references in PR descriptions are marked source=closing_keyword and are textual evidence, not verified GitHub closing relations. Inline review comments retain their raw diff context and source links.

`read_overview(state, days=30, repos=None)` returns status, conservative observed_at/coverage_since, repository counts/status/errors, daily lifecycle values and the latest 1,000 local events. `read_repository(state, repo, days=30)` also returns retained items and their evidence. The optional repos allowlist filters every overview aggregate and event; retained disabled repositories need not appear in the dashboard. Reading a missing database returns unavailable without creating files. The days filter limits events and charts, not retained issue inventory. SQLite WAL allows dashboard reads while collection commits. There is no automatic retention deletion.

## Dashboard deployment

Install `legacy/forge-status.py` and `legacy/dashboard.html` together in
`~/ops/forge-watchdog/`, and `forge/observability.py` in its `forge/`
package. Install the two deployment units in the user systemd unit directory.
The supplied service uses `~/ops/forge-watchdog/state`, matching the live
dashboard. Reload user units and restart `forge-status`; enable/start
`forge-observability.timer` to collect every ten minutes after each run completes.
The dashboard continues to work from saved data while GitHub is unavailable.

For an initial backfill, a larger explicit `--evidence-limit` may be used once;
subsequent service runs remain bounded to eight items per repository. The lock
prevents overlap. A terminated run resumes from committed repository/item state.
No collector command changes issues, labels, PRs, reviews or fleet workers.


### Issue cards and live activity

The dashboard projects each issue into a single Build, Review, or Audit workstation, with Working and Pending stacks. It joins only explicit same-repository PR references; a merged PR alone does not prove issue completion. Unlinked PRs and unknown stages appear under Unconfirmed work. Grey cards are waiting; role colors and card animation indicate observed work. Critical approval waits appear separately as issue cards in Needs attention. Motion is disabled when reduced motion is requested.

Worker activity is not sufficient to identify an issue when several issues or unresolved PR candidates share the role. Those cards remain pending with assignment uncertainty. Activity observations are sampled every thirty seconds, and the dashboard refreshes every twenty seconds. Dispatch timestamps are labeled as dispatch, not asserted execution start times.

The repository list uses the same issue projection. A unified Activity panel follows the chart; waiting work appears as compact icon stacks with counts; expanding a stack reveals its issues. Started work precedes ready work and held work. No aggregate task/PR action counts are used as issue throughput.

See [Reading the monitoring page](monitoring.md) for operator states and actions.
