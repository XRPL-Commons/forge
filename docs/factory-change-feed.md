# Factory change feed

The selected SQLite runtime serves `GET /api/factory/changes`. It uses the same
operator session, host and upstream checks as other status endpoints. The server
checks the session again after waiting, before returning data.

## Cursor and response

A factory board response includes `cursor`, captured in the same database snapshot
as its issues and jobs. Pass that value as `since`:

```text
GET /api/factory/changes?since=<cursor>&wait=25
```

`wait` is optional and accepts 0–25 seconds. The server returns when a committed
event sequence advances, or when the wait expires. It checks for a commit every
250 milliseconds without holding a database transaction while waiting. The
existing proxy timeout is 30 seconds; clients should allow 30 seconds too.

The response contains:

- `cursor`: the current instance identity and event sequence, treated as opaque by clients.
- `at`: server time when the committed response projection was produced.
- `events`: ordered headers containing `sequence`, `kind`, `at`, `unit_id`,
  `issue_id` and `job_id`. Raw event payloads and actor data are excluded.
- `issues`: changed public issue summaries keyed by issue identity. Bodies,
  historical reports and artifacts remain lazy detail reads.
- `board`: a current board response, including the same cursor and normal
  `ok`/`controls_token` fields.
- `resync`: true when starting without a cursor, changing instances, receiving a
  cursor ahead of the database, or exceeding the catch-up limits.

Catch-up is bounded to 100 events and 100 affected issues. On resync, event and
issue deltas are empty: replace the board and refresh any expanded detail from
its normal endpoint. Do not animate the omitted history. An idle response still
refreshes global observations and clock-derived fields. Expanded detail readers
refresh at least every 25 seconds, including while events keep arriving, for
observations that do not emit lifecycle events.

Invalid query syntax returns 400, an unselected legacy installation returns 409,
and a storage failure returns 503. Expired sessions use the normal 401 behavior.
The endpoint does not fall back to JSON state.

Inventory changes and admission-failure observations append `issue_observed`
events in their write transactions. Repeating an identical inventory observation
or the same failure does not append a new event. Existing unit and execution
transitions retain their own event kinds.

## Issue detail

`GET /api/issues?issue=<key>` includes these additional fields in the same snapshot:

- `jobs`: current or uncleaned worker metadata for the issue;
- `factory_issue`: the board card for its delivery, including recent attempt
  metadata and deployment outcome, or null for an issue without a delivery;
- `deployments`: repository deployment observations;
- `cursor`: the sequence captured with the detail response.

These fields remove the ordinary detail page's second `factory?issue` request.
Output remains a separate request. An open issue automatically reads its active
worker’s bounded live tail for the activity bubble and output panel. Historical
output is fetched when the operator opens it; the board never reads worker tails.
Concurrent live reads coalesce per job and cache for three seconds. One ten-second
deadline covers engine inventory, inspection and logs; unrelated jobs do not wait
on that read. Captured logs take precedence once the worker has been collected.

## Read cost and consistency

`Store.read_session()` shares one validated read-only connection and one snapshot
across nested configuration, repository and worker reads in a public projection.
The outer transaction ends before returning the response. It neither caches data
between requests nor allows a mutation through that store during the projection.
Cached status GETs (`/api/factory`, `/api/issues`, `/api/delivery`) own one physical
read connection for the request. Authorization rolls back its snapshot before
cache fingerprinting; projection starts a separate snapshot on that connection.
Every checkout revalidates the live schema and instance and restores the required
SQLite settings. Writes and independent nested reads use separate connections.
The HTTP server retains at most one idle read handle between successful requests.
Each request exclusively owns its borrowed handle; concurrent requests use separate
connections without waiting for that slot. Every snapshot closes its cursors and
rolls back before the handle can become idle. Failures close the handle. Selection
changes and server shutdown retire it; a changed database inode is reopened and
fully validated. Transfer between handler threads requires serialized SQLite.
This retains no authorization result or open transaction between requests. Long
polls and authentication callbacks do not use this lease.
The board starts from current memberships of nonterminal units, then applies
the canonical issue selector and ordering. Paused and gated members remain
candidates. An empty discovery-failure set skips its classification query in
the same snapshot; nonempty sets retain the canonical status checks.
The output projection closes its metadata snapshot before reading a live engine
tail. A long-poll wait uses autocommit reads and opens the response snapshot only after
the wait ends, so it does not prevent WAL checkpoint progress.

## Shared UI state

`useFactoryStore` keeps issue and job entities for the current Nuxt app and reuses
unchanged entity objects across responses. The board, header activity pill, drawer,
issue detail and factory strip share those records. Active consumers share one
cancellable long poll; hidden pages pause it and the last consumer releases it.

Mounted detail views refresh for their affected events, resyncs and the shared
25-second observation deadline.
Issue bodies and historical output stay lazy. Active issue output is shared by
the bubble and output panel. A second scoped factory request is reserved for an
explicit orphan-run link whose issue is no longer available in managed inventory.

When a viewer leaves or hides the page, the client aborts its poll. Nitro closes
the corresponding upstream request, and Python detects that socket closing
between its 250-millisecond waits. The handler releases its database connection
without building or returning an abandoned projection.

## Animation frames

The strip pairs each committed board cursor with its event headers. A new matching
cursor authorizes motion; the latest event timestamp, response timestamp and local
receipt time set its animation origin without assuming synchronized client clocks.
The projected views supply the start and end positions. A sequence is consumed once.
Initial views, resyncs, idle responses and batches containing any event older than the 1.5-second motion
window snap to current positions; the UI does not invent intermediate stages or
replay omitted history.

## Projection read ownership

Activity loads evidence for its bounded card page inside the existing SQLite
snapshot. Latest prerequisite checks use the recorded member and exact revision
index, including former memberships; unrelated retained checks are not scanned
for each card. Context, budget charges, candidate metadata, deployment evidence
and the last three events are collected for the selected page. The shared
lifecycle evaluator and immutable job scopes still determine status and worker
visibility. Detail bodies and artifacts remain separate reads.

Repository environment views select the latest scan and latest eligible completed
proposal in batches. A running or failed scan still retains its previous eligible
proposal. Saved repository views do not initialize GitHub credentials; an explicit
access refresh creates its client before parallel discovery. These read owners
last only for the current projection, without changing cache validity or readiness.

## HTTP timing evidence

Authenticated cached GETs (`/api/factory`, including `section=board` and
`section=completed`; `/api/issues`; `/api/delivery`) return `Server-Timing`.
The UI proxy forwards it on both 200 and 304 responses. All values are durations
in milliseconds; the header contains no repository, issue, user, or token data.

- `projection` measures the projection callback, when it actually runs.
- `cache_miss` measures the full cache response operation when that callback runs,
  including projection, serialization, state fingerprinting, and lock waiting.
- `cache_hit` measures reuse of a serialized response without projection.
- `cache_validated` measures an unchanged validator check that skips both the
  projection and the serialized body lookup. This can succeed after eviction.
- `conditional` measures the final conditional-response decision when HTTP 304
  is returned. It does not replace or hide any projection/cache measurements.
- `request` runs from GET handler entry through response preparation, including
  authorization. It excludes request parsing, socket transfer, network latency,
  UI proxy work (including compression), and final physical connection release
  after sending the response. Nested durations must not be added.

A cache miss does not establish a cold process: expiration, eviction, changed
state, and a different query can also cause one. Record cold-process evidence
separately. While normally signed in, inspect these headers in browser DevTools
Network for the board and completed-history requests. Keep browser/network
measurements separate from these server durations. No cookie export or extra
agent grant is needed.

For bounded page diagnostics, add `?timings=1` to the signed-in page URL
(or `&timings=1` if it already has a query). Ordinary UI reads then write up to
100 `[forge-http-timing]` debug entries to that tab's console, including the
initial server-rendered board fetch. The samples live in request-scoped Nuxt
state and hydrate with that page; separate requests never share them. The same
100-sample limit covers server-render and browser reads together. Reload to start
a new window; remove the parameter to stop collecting. Each entry contains a
fixed view label, `source` (`server_render` or `browser`), HTTP status, numeric
server durations and `fetchMs`. Board, completed history, issue lists and issue
details have separate labels.
No URL, issue identifier, response payload, cookie or token is recorded. Only
these timing samples are included in the opted-in page's hydration state; none
are persisted to disk or sent to another service. This does not generate additional requests
or change cache behavior. Use console debug/verbose visibility to inspect it.

Group samples by view and actual cache classification before calculating
percentiles. Keep 200/304 status as another dimension: a 304 may still have
projected a response. Keep `source` separate as well. `fetchMs` runs immediately
around the actual `$fetch.raw` call through response parsing. For `browser`, it
includes browser/proxy/network work; for `server_render`, it measures Nuxt's
server-side API fetch and excludes page rendering, hydration, and the browser's
document transfer. It is not end-to-end page latency. `serverMs.request` is the
Python handler interval described above. Retain
sample counts, and do not claim an unobserved cache class was qualified.
These are successful timed-response samples: failed requests, timeouts, aborts
and responses without the timing header are absent. Record failure and timeout
counts separately during qualification so a latency percentile does not hide
unsuccessful requests.
