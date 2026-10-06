# Runtime state source inventory

This inventory accompanies the inactive importer. It is not a declaration that
all writers have moved to SQLite. Source references describe the current modules;
phase 7 must add a static/runtime check that rejects surviving operational JSON
writes after cutover.

| Domain | Current writers/readers | Source files | Import status |
| --- | --- | --- | --- |
| Pipeline | `factory/ledger.py`, `factory/supervisor.py`, `factory/control.py`, `factory/issue_actions.py` | `state/factory/factory-ledger.json`, operator/check requests, supervisor heartbeat | Core, controller context, failure evidence, provider/memory facts and requests mapped; runtime transitions and external-effect reconciliation pending |
| GitHub issue inventory | `factory/issues.py`, `factory/control.py`, `factory/discussions.py` | `state/issues/inventory.json` | Issue facts/revisions, repository sweep metadata, collaborators and partial PR summaries mapped; partial summaries never fabricated into exact candidate observations |
| Operator actions | `factory/issue_actions.py` | `state/issues/actions.json` | One request row per decision, preserving opaque expected revision, status and plans; effect dispatcher pending |
| Discussions | `factory/discussions.py` | `state/issues/discussions.json` | Threads, paged messages, proposal metadata, executions and explicit artifacts mapped |
| Deployment observations | `deployments.py`, `factory/discussions.py`, `factory/control.py`, `repository_setup.py` | `state/deployments.json` | Per-repository profile, observation/proposal and auxiliary execution mapped |
| Environment | `repository_environments.py`, `factory/control.py` | `state/environment-scans.json`, `state/environment-proposals.json`, runtime configuration revisions | Scans, proposals, per-unit choices/overrides and versioned configuration mapped |
| Repository access/setup | `settings.py`, `repository_setup.py`, `setup_service.py` | `state/github-access/{access,repositories,sync}.json` | Separate role/repository observations, sync receipt and repository setup report rows |
| Setup/update | `onboarding.py`, `host_setup.py`, `host_update.py`, `setup_service.py`, `operator_bootstrap.py` | `state/onboarding/setup.json`, `state/setup-service.json`, setup/runtime/host runtime configuration | Setup identity/generation, configuration and separate probe executions mapped; runtime setup adapter pending |
| Operator identity/auth | `operator_auth.py`, status request audit writer | `state/operator-auth/auth.json`, `requests.jsonl` | Identity configuration, credential reference, sessions, flows, original epochs and request audit mapped; no revived authorization |
| Agent/client operations | `client_repositories.py`, `client_connections.py` | `state/client-operations/operations.json`, connection state | Grants, consumed device codes, refresh replay history, rate limits and operations mapped |
| Notifications | `notifications.py` | `state/notifications/outbox/*.json`, delivery status | Outbox channel rows and retained delivery receipts mapped; an unknown delivery-status source still blocks qualification |
| Configuration | `settings.py`, `factory/config.py`, `factory/verification.py`, onboarding/setup/update adapters | `factory.json`, `runtime.json`, `host-runtime.json`, `setup/runtime.json` | Versioned configuration rows; identity secret staged separately with a verified private reference |
| Activation/pause markers | `onboarding.py`, `setup_service.py`, `host_setup.py`, `factory/control.py` | `state/*.enabled`, `state/factory-paused.json`, login marker | Named runtime controls retain opaque source generation and marker facts |
| Initialization markers | `state.AtomicStore` | `*.initialized` | Captured with source; a marker without its store blocks capture |
| Credentials | `credentials.py`, `factory/codex_auth.py`, provider login helpers | Credential directories, credential index, staged provider auth | Remain credential material, outside runtime row migration; refs must be preserved |
| Temporary IPC | `state.py`, settings locks, runner locks | `*.lock`, `.pending-*`, process locks | Not authoritative snapshot sources; no import of interrupted atomic-write temporaries |
| Independent observer | `observer/` | `state/observer/` and observer-private mirror state | Independent domain; any included evidence is not selected as controller runtime state |
| Deployment diagnostics | Historical operator/tool receipts | `state/*-deployment.json`, `state/deploy-*.json` | Diagnostic evidence only |

Capture discovers recognized JSON/JSONL/initialization/activation files beneath
`state/` and `setup/`, plus the three top-level configuration files. It rejects
symlink files/directories and checks the complete selected file set before and
after copying under maintenance. A new storage format or marker suffix requires
updating this inventory and capture classification before that domain can cut over.
Unclassified captured files remain visible in the source manifest and prevent
completion through a `domain_mapping_pending` finding.

No adapter may read an imported whole-document archive as its new runtime store.
The archive exists to prove lossless import and diagnose mapping errors. New
runtime paths must use normalized row queries and typed transitions.
