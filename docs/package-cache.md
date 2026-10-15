# Shared package cache

Forge workers install public npm and PyPI packages through the existing
read-only registry gateway. Each job retains its own isolated network and
gateway. The gateway connects to `forge-registry.service` over a Unix socket;
workers receive neither this socket nor the cache directory.

The service fetches only the gateway's approved public registry hosts, using
GET or HEAD. It strips credentials, checks every resolved IP before connecting,
and refuses redirects outside those hosts. Provider tunnels remain separate.
Metadata is fetched live and package links are rewritten by the job gateway.

Only complete successful package archives with explicit public freshness are
cached. Cache identity includes the exact host, path, query, Accept header and
identity encoding. Private, no-store, no-cache, cookie-bearing and unsupported
Vary responses are not cached. Freshness accounts for upstream Date and Age,
and is capped at one day. Errors, redirects, metadata and partial downloads are
not cached. A cache does not replace project lockfiles or integrity checks.

The persistent archive budget is 2 GiB, with at most 4,096 entries and least
recently used eviction. Two concurrent requests allow at most another 256 MiB
of temporary downloads. Metadata responses remain limited to 8 MiB and each
archive to 128 MiB. Downloads have a five-minute deadline. Concurrent requests
for the same archive share one fetch; incomplete files are never published.

## Installation, updates and qualification

`forge install` and `forge migrate upgrade` install and start the service and
check its socket before starting the supervisor. A source update restarts it
through the normal managed-service lifecycle. The service runs as the Forge
account, stores data under `<root>/registry/cache`, and exposes no TCP port.
Reset includes the service in ownership checks and stops it before archiving
the installation. No separate manual setup is required.

The existing `forge setup qualify-boundary --ssh HOST --apply` qualification
runs npm and pip downloads through the new transport. It then disables the
disposable cache's upstream and downloads the npm archive from a second job,
proving reuse across isolated jobs. Its receipt includes
`registry_cache_cross_job_offline_hit`. It also verifies that the worker cannot
see the socket mount. Qualification uses disposable storage and does not alter
the installation's cache.
