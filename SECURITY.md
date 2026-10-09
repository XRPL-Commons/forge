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

## Runtime trust boundaries

“Live” below means implemented by the supported factory runtime. Host and provider
qualification still apply to each installation. A compromised service account,
Docker daemon or host kernel is outside the worker isolation boundary.

| Boundary | Principal and credentials | Enforcing control and limit |
| --- | --- | --- |
| Browser → Nitro UI | Authorized operator; browser holds a session cookie and an action token | Verified HTTPS, allowed Host, same-origin mutation checks and bounded JSON requests. Python validates the operator session and action token. Before sign-in is configured, private-network access and the local control capability remain trusted. |
| Nitro → Python API | Trusted UI proxy; holds only the private upstream secret, not App keys or the identity-app secret | Python binds to loopback and checks the upstream secret and trusted Host. The UI forwards only allowed headers and routes. This is a service boundary, not protection from the host service account. |
| External CLI/agent → Forge | Operator-approved connection, scoped to capabilities and repository identities; client credentials in the local OS keyring | Verified HTTPS, expiring credentials, server-side token hashes, explicit approval and revocation. Browser cookies cannot substitute for client credentials. Local `gh` administration tokens are used against GitHub locally and are never uploaded to Forge. |
| Local administration CLI → host | Operator's SSH account, with permission to manage Forge and its Docker services | SSH host verification and host filesystem permissions. `forge server setup` transfers only the identity-app secret required by that setup; it is hidden input, not an argument. `forge migrate` performs the explicit maintenance/cutover protocol. Host administration is more privileged than a scoped Forge connection. |
| Controller → private state | Dedicated service account; owns the selected SQLite store, App keys, provider credentials and identity-app secret | Private directories/files, schema and instance checks, qualified SQLite, transactions and revision checks. Credentials remain separate private files; database records reference them. The browser and worker do not receive database access. |
| Controller → Docker worker | Trusted controller owns the Docker socket; worker receives an assignment and short-lived repository/role credentials | Disposable volume, UID 1000, read-only root, dropped capabilities, no-new-privileges, private namespaces, seccomp and the qualified runner AppArmor policy. No host directory, Docker socket, App private key or controller token is mounted into a job. Docker access itself is administrative authority. |
| Worker harness → repository tools | Provider client can authenticate; repository code is untrusted | Native tool sandbox, image-owned policy and credential scrubbing restrict repository tools. Each adapter needs a real sandbox canary; a model success message is insufficient. Resource limits do not establish credential isolation. |
| Worker → network | Worker on an internal Docker network; separate egress proxy has no role App key | HTTPS CONNECT allowlist on port 443 and rejection of private/non-global destinations. The runner refuses a non-internal worker network. An allowed public service can still receive data a worker sends to it. |
| Controller/worker → GitHub | Four role Apps; installation tokens scoped to the selected repository and role | Worker Review/Audit/Recovery tokens are read-only; Build can publish work. The controller validates candidate evidence and uses its own token for merge. GitHub protections and expected-head checks apply. App grants are repository-scoped, not per-issue or per-branch restrictions; prompts cannot enforce narrower API authority. |

Implementation: [proxy guards](ui/server/utils/proxy.ts),
[API admission](legacy/forge-status.py), [client connections](docs/agent-connections.md),
[host sign-in setup](docs/cli-server-setup.md), [runner](forge/factory/runner.py),
and [runtime configuration and GitHub gates](docs/factory-runtime.md).

## Isolation status

| Mechanism | Status | Meaning |
| --- | --- | --- |
| Disposable Docker worker, native tool sandbox, runner seccomp/AppArmor and internal egress network | Live | Used by the supported factory runner after host/provider qualification. |
| SQLite runtime authority, managed launcher and legacy-writer fence | Live after explicit selection | Missing, incompatible or invalid selected state stops startup; it does not select JSON as a fallback. |
| `safe-build`, memory admission, cgroup limits and build locks | Live resource controls | Bound resource usage and execution time. They do not isolate credentials or confer authorization. |
| Dedicated native client accounts and executable profiles from `deploy/provision_isolation.py` | Staged, inactive factory alternative | Provisioning artifacts and synthetic canaries exist. The supported runner does not launch jobs through these host accounts. The `unconfined` executable profiles permit user namespaces; they are not application confinement. |
| Independent observer isolation/deployment | Deferred, separate rollout | Not established by factory deployment. Keep #111 deferred until core qualification is complete. |
| tmux fleet supervisor and its `legacy` onboarding phase | Retired | Supported launchers start the durable factory; old phase values become setup. There is no supported tmux startup path. |
| JSON lifecycle/configuration stores | Pre-cutover compatibility only; retired on selected instances | The selected runtime fences old readers/writers. Frozen pre-migration data is private diagnosis material, not a recovery backend. |

See [isolation status and staged provisioning](docs/isolation.md) for the separate
native-account proposal. SQL execution adapters are part of the live controller;
they must not be confused with that staged host-executor proposal.

## Storage, retention and recovery

The selected store uses WAL with `synchronous=FULL`, private `0700` directories and
`0600` files. The immutable `state/storage` marker binds its location and instance.
Managed `forge.launch` entrypoints select a qualified SQLite library and validate
schema checksums before opening runtime adapters. Selected state has one authority;
staging databases cannot accept normal runtime writes.

Migration is explicit and one-way. The CLI drains workers, stops writers, captures
private source data, validates relational/source/public-view parity, then publishes
the selection marker. An interruption resumes the journal; there is no JSON
switch-back. The pre-migration copy is kept only for diagnosis. See the
[migration procedure](docs/store-migration.md).

Events and structured results are retained. Eligible raw job logs expire after
30 days through bounded supervisor pruning; active work, current evidence,
recovery/cleanup and pinned references are retained. Audit history is append-only
and survives reset. Private artifacts remain sensitive even after best-effort
redaction. Back up the selected database using a consistent SQLite backup, with
its private instance metadata and referenced credential files. Copying only the
main database while WAL writers run is not a coherent backup. Do not initialize
an empty store over unreconciled workers or GitHub publications.
