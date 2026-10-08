# Runtime state source inventory

The application selects SQLite through the private `state/storage` marker.
Before selection, the legacy paths remain available for capture and draining.
After selection, adapters use the rows below; legacy operational JSON writes fail.
Production activation is a separate CLI operation, described in
[the migration guide](../store-migration.md).

| Domain | Selected runtime | Legacy source |
| --- | --- | --- |
| Pipeline | `factory/sqlite_runtime.py` composes execution, observation, requirements, recovery, verification, effects and dispatch controllers | `state/factory/factory-ledger.json`, requests and heartbeat |
| Issue inventory | `factory/store_inventory.py`, `store/queries.py`, `factory/store_views.py`; revision facts, sweep cursors and bounded presentation rows | `state/issues/inventory.json` |
| Operator actions | `factory/store_operator.py`; actor-bound requests and external-effect receipts | `state/issues/actions.json`, operator/check requests |
| Discussions | `factory/sqlite_discussions.py`, `store/discussions.py`; threads, paged messages and owned executions | `state/issues/discussions.json` |
| Deployments | `sqlite_deployments.py`, `store/auxiliary.py`; profiles, observations, proposals and execution artifacts | `state/deployments.json` |
| Environment | `sqlite_environments.py`, `store/runtime_requirements.py`; scans, proposals, per-unit choices and configuration revisions | Environment scans/proposals and runtime defaults |
| Repository access/setup | `sqlite_settings.py`, `repository_setup.py`; role/repository observations, sync receipts and protection checks | `state/github-access/{access,repositories,sync}.json` |
| Setup/update | `sqlite_onboarding.py`, `sqlite_setup_service.py`, `host_update.py`; setup generation, probes, activation and configuration history | Onboarding, setup heartbeat and host configuration |
| Operator identity/auth | `sqlite_identity.py`; identity configuration, private credential reference, sessions, flows, original epochs and request audit | `state/operator-auth/auth.json`, `requests.jsonl` |
| Agent/client operations | `sqlite_identity.py`; grants, device codes, replay history, rate limits and operations | Client connection/operation stores |
| Notifications | `store/notifications.py`; one outbox row per event/channel, attempts and delivery receipts | Notification outbox and delivery status |
| Configuration | `store/configuration.py`; versioned rows for live/setup runtime, repository settings and host records | `factory.json`, `runtime.json`, `host-runtime.json`, `host.json`, setup configuration |
| Activation/pause | Named `runtime_controls` rows with original generation facts | Activation, pause and login markers |
| Initialization | Import provenance and store identity; orphan legacy initialization markers block capture | `*.initialized` |
| Credentials | Private credential files remain separate; configuration stores references | Credential directories, credential index and staged provider auth |
| Temporary IPC | Process locks and runner control files; no authority imported from interrupted atomic writes | `*.lock`, `.pending-*` |
| Independent observer | Separate observer domain; deferred in #111 | Observer-private state and mirror |
| Deployment diagnostics | Immutable diagnostic evidence | Historical deployment receipts |

## Cutover boundary

`legacy_writers.py` gives operational JSON writers a shared process lease. The
migration CLI takes the exclusive lease after draining and stopping services,
and holds it from capture through storage selection. A durable maintenance
journal keeps writers blocked after an interruption. Existing adapter objects
recheck the boundary before publication, including notification delivery.

Capture discovers recognized JSON, JSONL, initialization and activation files
beneath `state/` and `setup/`, plus the four top-level configuration files. It
rejects symlinks and checks the selected source set and digests. Unclassified
files remain visible in the manifest and block activation.

The importer preserves original rows and evidence. Fresh migration reconciliation
checks active issue and candidate identities before resuming, rebuilding or
closing work. Interrupted operator intentions remain attributed records and
require an explicit decision where automatic continuation would change intent.
Truncated cached bodies remain marked incomplete until a fresh GitHub read.

Whole-document import archives support parity checks and diagnosis. Runtime
adapters use normalized rows and bounded artifacts; they do not reconstruct a
writable JSON ledger. Tests exercise the selected public paths, resumable cutover,
concurrent writers, and retained identity and lifecycle facts.
