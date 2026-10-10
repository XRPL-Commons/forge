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

### Repository readiness snapshot

A subsequent paired comparison on schema 28 measured 25.102 ms before and
22.603 ms after extracting `Settings.repository_views(runtime=...)`. The full
Settings response and all three Activity sections remained identical. This
comparison used the same twelve-call alternating procedure; server load varied
between runs, so separate benchmark medians should not be combined.

The repository reader now uses the projection's already validated runtime for
merge policy and environment readiness. It shares the Settings readiness
implementation and omits the unrelated sync/header reads. The full Settings
endpoint retains those fields and behavior. No state is cached between requests.

### Migration catalog lifetime

Each `Store` now loads the installed migration SQL and checksums once into an
immutable tuple. All of that handle's connections still read and validate the
applied migration rows, schema metadata, instance anchor and private paths.
Database state is not cached. An already running handle keeps its release's
schema contract even if files on disk change.

The explicit upgrade command constructs a fresh Store from the pinned target
tool. Prefix validation, migration application and final validation use that
same catalog. Runtime services restart after the upgrade. Old handles reject a
database upgraded beyond their catalog; fresh handles load the new installation.

A local paired comparison on qualified SQLite 3.53.1 used a populated schema-28
fixture with one working issue. Each version ran 42 times in alternating order;
the first two samples were excluded. Board and completed objects were identical.

| Local operation | Median before | Median after |
| --- | ---: | ---: |
| Open, validate and read one snapshot | 3.089 ms | 2.386 ms |
| Board projection | 6.218 ms | 5.554 ms |
| Completed projection | 4.359 ms | 3.686 ms |

These local fixture results measure the catalog change against release
`f883620`. They do not establish production or authenticated HTTP latency;
normal authenticated `Server-Timing` measurements qualify the deployed result.

### Completed selection from terminal memberships

Completed-page selection and counting first collect distinct issue IDs from all
memberships of terminal units, including former memberships. The existing
`issue_selection_facts` retains the indexed latest-membership and terminal
identity predicates. The shared `IssueSelection` query supplies canonical
status and blocker reasons before filtering (see below). This avoids classifying
the unrelated untracked inventory. Existing unit-state, unit-membership and
issue-identity indexes serve this narrowing; it introduces no stored status or cache.

A local synthetic comparison against `7ab6023` used SQLite 3.53.1 and 24
alternating calls per version, excluding two warmups. The larger fixture had
1,035 issues, 115 terminal units and four active jobs. Both completed-page
objects (50 then eight cards), order, counts and cursors matched exactly.

| Local completed first page | Median before | Median after |
| --- | ---: | ---: |
| 1,035 issues | 11.372 ms | 10.224 ms |
| Two issues | 3.863 ms | 3.900 ms |

Focused lifecycle coverage also preserves removed closed members, released and
dissolved members in newer deliveries, reopened issues and closed untracked
issues. Former memberships must remain in the candidate input; final canonical
predicates determine whether they match. These are local projection results,
not authenticated HTTP evidence, and the larger result remains above 10 ms.


### Dependency-aware selection before pagination

Schema 031 renames the former public-status SQL view to `issue_selection_facts`.
Its `indexed_status` and `indexed_reason_code` columns are coarse selection
facts: SQL edge existence cannot decide whether recursively pinned child outputs
are still valid. Callers use `IssueSelection` in the same read snapshot as their
page projection. It starts from current dependency edges and approved-delivery
identities, loads only members of those affected deliveries within the request's
repository/search scope, and uses the existing lifecycle/output evaluator and operator status projector.
It joins those exact statuses and reasons into the SQL query before filtering,
counting, ordering and pagination. No lifecycle status is persisted or cached.

Members without dependencies are included because a delivery-wide gate can
apply to them. Approved deliveries without edges also need exact child-job
scope, so a running child does not make its idle siblings appear to be working.
Removed/terminal memberships keep the existing historical identity rules; terminal pages do not evaluate active dependency graphs. Bodies and
artifacts remain lazy, and already-loaded member contexts are reused by page
rendering. This migration changes a read contract only; it neither changes
outputs nor grants dispatch/publication authority. Release qualification for 031
must include data preservation, schema integrity and idempotent replay.

### Collect delivery gates once per read snapshot

Issue pages used to collect the same delivery's gates once for each member.
Each collection walks all current members and validates their recursive output
references. A 40-member star therefore repeated 1,600 member dependency checks
while selecting the page.

`IssueGateFacts` now owns those facts during one read projection. It collects
once per unit and uses the same canonical member/candidate scope and priority
function for each issue. `IssueSelection` retains this owner across its bounded
context batches and final page rendering. The owner is discarded with the read
snapshot. Lifecycle and admission calls still collect fresh facts, and recursive
output validation is unchanged. This requires no schema migration or stored
cache.

A local synthetic comparison against `3f08658` used 14 alternating calls,
excluding two warmups. Full board and issue-page responses matched exactly.
Projection measurements include opening and validating the read snapshot.

| Local board fixture | Median before | Median after |
| --- | ---: | ---: |
| 1,035 ordinary issues | 7.264 ms | 7.256 ms |
| 12-member dependency chain | 18.118 ms | 7.780 ms |
| 40-member dependency star | 34.863 ms | 9.587 ms |

These are synthetic projection measurements, not deployed authenticated HTTP
latency. The existing output evaluator still walks recursive prerequisite
references; the change removes repeated gate collection without introducing a
second validity evaluator.
