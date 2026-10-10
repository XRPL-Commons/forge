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
