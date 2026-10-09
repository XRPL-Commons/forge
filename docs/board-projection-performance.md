# Board projection measurements

## 2026-10-10 — schema 28

A frozen production copy, upgraded from schema 27, gave the following paired
measurements of `PipelineViews.factory(section='board')` with Settings repository
readiness. These are projection times, not authenticated HTTP latency.

| Change | Warm median before | Warm median after |
| --- | ---: | ---: |
| Scoped blocker lookup and one harness catalog per runtime validation | 41.014 ms | 27.911 ms |
| Above plus the terminal-event index | 42.180 ms | 22.073 ms |

Each comparison alternated twelve calls per version in one process, using the
same clock, configuration and qualified SQLite runtime; medians exclude the first
call. The copy contained 1,035 issues, with four board cards and one board job.
The board, completed and runners objects were identical. All 1,035 canonical
issue-index rows and all existing data in 141 tables were unchanged. Schema
27→28 passed integrity, foreign-key and repeated-apply checks on a new private
server-local copy; the retained source copy and live store were unchanged.

The board selector previously ranked blockers across the whole backlog. The
correlated lookup retains the exact predicates and priority/registration/time/ID
ordering, using the unit's unresolved-gate index. A partial completion-event
index serves both terminal counters and history pages without scanning retained
intermediate events. Recursive runtime validation shares one current installed
harness catalog within the call; subsequent calls reread the installation.

The single-digit HTTP target remains unqualified. Use the authenticated
`Server-Timing` evidence described in [the change feed guide](factory-change-feed.md#http-timing-evidence)
to distinguish projection work, cache behavior and request preparation from
network and proxy time.
