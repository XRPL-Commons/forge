# Operator UI performance

## Data lifecycle

`useResource` owns a bounded, in-memory cache per browser app and per server
request. It shares requests and a polling timer by resource key. Server rendering
loads the initial view and transfers it in Nuxt's payload. Hydration adopts that
snapshot; mounting a fresh view does not immediately fetch it again. The hydration
snapshot is then discarded, leaving the bounded resource cache as the owner.

Navigation reuses cached data immediately and refreshes expired entries in the
background. Entries include the whole query string, so repositories, filters and
issue keys stay separate. Idle entries expire after five minutes, with an 80-entry
limit. Hidden tabs stop polling. Collapsed sections stop polling and retain their
last value for reopening. Relative ages use `useClock` to match the server render.

`useForgeApi` retains each GET's ETag and payload in this same cache. Python checks
Host/upstream authorization before responding to validators. Unchanged reads return
304 with no body; Nitro forwards the validator and the browser composable reuses
its payload. Responses use `no-store`: this is application memory caching, not a
shared HTTP cache or persistent browser storage. A changed action token changes
the ETag as well.

Operator actions invalidate cached freshness and validators before and after the
request. Generations prevent an older response from replacing a newer action's
result. Reset and rejected authorization clear cached values. Existing data remains
visible with an error when a refresh fails.

## Smaller and deferred reads

- `/api/factory?section=board` contains active cards and small counts/worker
  summaries. Investigation cards use `section=runners`, fetched when their line
  is opened. Recently completed reads
  `section=completed&limit=10&before=<cursor>` a page at a time, newest first by
  merge or close time, as the operator scrolls; the answer carries `next`, the
  cursor of the following page, or null after the last. Without `limit` the
  section still returns every completed issue. Header counts use the board
  summary.
- Issue cards draw their cube as one still frame per card, only once the card
  is on screen (one shared IntersectionObserver), and redraw only when its look
  changes. The only motion is an occasional blink of one visible cube from one
  shared timer, and none with reduced motion or a hidden tab.
- `/api/factory?issue=<key>` returns only that issue's factory rows. Reports and
  captured output stay out of every factory projection.
- `/api/settings?summary=1` preserves repository state and per-App access counts,
  while omitting protection reports, database evidence and deployment proposals.
- Worker output uses a cached resource that refreshes only while expanded.
- Issues remain server-paginated in groups of 50, with background inventory refresh.
- Ledger reads take a shared lock and validate an independent snapshot. They do
  not rewrite the state file. First-use initialization, missing/corrupt-state
  failures and operator decision fences still apply.

## Measuring

Build the UI and start it against the fixture API or a live read-only endpoint,
then run from the repository root:

```sh
FORGE_BENCH_URL=http://127.0.0.1:3999 \
FORGE_BENCH_OPTIMIZED=1 \
FORGE_BENCH_OUTPUT=/tmp/forge-pages.json \
node ui/tools/measure-pages.mjs
```

Set `FORGE_BROWSER_PATH` if Chromium is not installed through Playwright. The
script only navigates/read-fetches existing pages; it does not operate controls.
It records first data appearing in the DOM, response-start time, HTML/API bytes,
whether the initial HTML contains data, and conditional response status/body size.
Compare the same routes, data and environment. Single cold-navigation samples are
indicative; tailnet latency and active workers can affect live timings.
