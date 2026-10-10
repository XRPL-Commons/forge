# Measured execution resources

The lazy **Combined delivery** panel and `forge repo delivery OWNER/REPO NUMBER`
JSON show resource evidence for each physical worker execution. This covers
builder planning, integration, delegated children, review, audit and recovery.
Repository triage and discussion workers use separate execution domains and do
not yet publish these measurements.

## What the numbers mean

- **Wall duration** is Docker's recorded finish time minus its start time, from
  an ownership-verified terminal container inspection. It excludes time waiting
  for admission. A worker captured while still running, a missing container or
  invalid timestamps has no measured final duration.
- **CPU** is cumulative CPU seconds consumed by the container at the sample
  time. It is not CPU percentage or a final total.
- **Memory** is current usage in bytes at the sample time, including cache. It
  is neither peak memory nor the configured memory limit.

Every CPU/memory sample retains the engine timestamp and `docker_engine_stats`
provenance. Capture does not change that timestamp. The latest collection
attempt has its own time and availability status. If collection fails, the
previous numeric sample remains visible with its original time and is labeled
as a retained sample. Missing counters are null; a measured zero remains zero.
A short execution may finish before any live sample and therefore have a wall
duration without CPU/memory measurements.

## Collection and storage

The supervisor requests one bounded sample during each normal lifecycle
inspection of a worker, including while it is running. Other inspection calls and UI reads do not trigger sampling. The
request uses the exact full container ID after checking execution and slot labels,
and connects to the same explicitly configured Unix engine socket used for
container operations. It does not inherit a Docker context or `DOCKER_HOST`.

The sampler negotiates an API version through `/version`, requires one-shot
stats support (API 1.41 or newer), and selects at most API 1.51. Version and stats
requests share a two-second deadline, shortened by any existing inspection
deadline; each JSON body is bounded to 64 KiB. Unsupported versions, absent
counters, malformed data and transport failures make resource collection
unavailable without fencing or failing a healthy worker.

Schema 032 adds compact observations keyed by physical job and exact container,
and an immutable resource receipt committed atomically with the existing worker
capture. Worker results and logs retain their existing evidence contract. The
public projection reads the small numeric receipt without loading results or
transcripts. Historical jobs are not backfilled with guessed usage. There is no
telemetry service, background sampling loop, global cache or change to resource
admission limits.

## Engine contract

Docker documents that stopped containers have no stats and that raw API memory
usage includes cache, unlike the CLI's adjusted display. See
[Docker stats](https://docs.docker.com/reference/cli/docker/container/stats/) and
[Engine API version history](https://docs.docker.com/reference/api/engine/version-history/).
Cumulative CPU counters are nanoseconds; the host converts them to seconds.
[Moby's stats types](https://github.com/moby/moby/blob/v28.5.1/api/types/container/stats.go)
and [Linux collector](https://github.com/moby/moby/blob/v28.5.1/daemon/stats_unix.go)
define those counters. The
[one-shot response path](https://github.com/moby/moby/blob/v28.5.1/daemon/stats.go)
can omit the response ID: the exact request target supplies identity, and any
nonempty conflicting response ID is rejected.
