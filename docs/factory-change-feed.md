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
Output remains a separate request made only when the operator opens a run.

## Read cost and consistency

`Store.read_session()` shares one validated read-only connection and one snapshot
across nested configuration, repository and worker reads in a public projection.
The outer session closes before returning the response. It neither caches data
between requests nor allows a mutation through that store during the projection.
A long-poll wait uses autocommit reads and opens the response snapshot only after
the wait ends, so it does not prevent WAL checkpoint progress.

## Shared UI state

`useFactoryStore` keeps issue and job entities for the current Nuxt app and reuses
unchanged entity objects across responses. The board, header activity pill, drawer,
issue detail and factory strip share those records. Active consumers share one
cancellable long poll; hidden pages pause it and the last consumer releases it.

Mounted detail views refresh for their affected events, resyncs and the shared
25-second observation deadline.
Bodies and output stay lazy. A second scoped factory request is reserved for an
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
