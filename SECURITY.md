# Security

Do not open a public issue with exploit details, credentials, private repository
content, or unredacted worker output. Use GitHub's private vulnerability reporting
for this repository when available. If it is unavailable, request a private
reporting channel from the maintainer without including sensitive details.

Forge is experimental operator software. Coding agents execute tools with the
credentials and filesystem access their deployment grants. Workers run in
disposable containers with repository-scoped credentials; see
[implementation status](docs/implementation.md) and [isolation](docs/isolation.md)
for what those boundaries do and do not establish.

The dashboard exposes private operational details. Existing installations retain
trusted-private-network access until [operator sign-in](docs/operator-sign-in.md)
is configured. Configured instances require GitHub operator sign-in for every
operational API read and mutation. Use verified HTTPS and an explicit operator
allowlist before permitting additional users to reach the dashboard. The local
host account and UI-to-API proxy secret remain trusted; authentication does not
protect against a compromised controller host. Critical changes require qualifying human
approval; an audit PASS, a shared agent identity, or a cached summary is not human
approval. Do not weaken ownership, current-candidate, or cleanup checks to increase
throughput.
