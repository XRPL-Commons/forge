# Issue conversations — implementation reference

For the operator walkthrough, read [Talking to a worker](issue-conversations.md). The storage description below applies to selected SQLite installations; qualification dates describe historical checks, not a guarantee about a future release.

An issue's **Discuss blocker** control opens a durable conversation. The agent receives the issue, candidate identity, the four latest attempt summaries, recent decisions and twelve recent conversation messages. It has a read-only repository credential and its own disposable runner. It can explain findings and propose an available issue action. **Apply this decision**, or an authenticated exact close command described in the operator guide, submits a stable operator request through normal controller validation. The discussion worker cannot approve a merge or security exception.

## Historical session continuity qualification (26 September 2026)

Checked the actual installed image `forge-runner:stuck-recovery` without credentials or network access:

| Provider | Installed CLI | Native capability | Existing Forge artifact | Implementation |
| --- | --- | --- | --- | --- |
| Codex | 0.153.4 | `codex exec resume SESSION_ID` | Fresh `exec` per assignment; disposable home removed on reset, no durable session mapping | New discussion with saved evidence |
| Claude | 2.1.273 | `--resume`, `--fork-session` | Adapter explicitly uses `--no-session-persistence` | New discussion with saved evidence |

CLI help establishes the flags, not successful native resumption. Native resume is deliberately **not qualified or enabled** here. Authentication and session history are different: existing ChatGPT and Claude logins are retained, but sharing a login cannot restore deleted conversations. Old issues and unavailable sessions always have the reconstructed-context route.

The latest attempt's provider is preferred if verified and available; another connected provider can answer when it is not. The UI identifies the source attempt and labels reconstructed context. No active worker conversation or worktree is modified. A future native-resume implementation would need explicit issue/attempt/session binding, selective transcript persistence excluding credentials, expiry/deletion, isolated forks and real tests across cleanup/restart. Never copy a provider home directory as a session archive or resume `--last` on a shared account.

## Lifecycle and limits

In the selected runtime, SQLite discussion rows store conversations and retained job intent; the pre-cutover compatibility implementation used `state/issues/discussions.json`. The selected runtime does not read that file as a fallback. One discussion worker runs at a time, after productive dispatch. It obeys existing memory admission and provider leases, uses a ten-minute execution deadline and gives up queued admission after ten minutes. It never consumes issue failure attempts. Setup/reset cancels discussions before touching accounts. Cleanup failure retains the job and lease until absence is verified.

The UI polls bounded snapshots, including public assistant text emitted so far, every 2.5 seconds while open; this is incremental delivery, not token-level SSE. Private reasoning and tool input/output are excluded. Reopening restores server history; older messages paginate twenty at a time. Inputs are limited to 4,000 characters, final answers to 12,000, reasons to 2,000 and retained conversations to 1,000 messages per issue. Twelve recent messages and bounded attempt summaries form the handoff; earlier turns remain readable but may be absent from the prompt.

## Presence: someone joins, works, answers or leaves

The read also returns `presence`, recorded in the discussion record only (never the factory ledger):

- `finding`: no worker yet, with `waiting_since` and the launch logic's real `reason` and `code`: `busy` (one discussion runs at a time), `paused`, `disabled`, `pipeline` (pipeline work launches first), `capacity` (memory), `not_ready`, or the provider cooldown's own code (`blocked_auth`, `blocked_quota`) with `until` and a link to Settings → Coding accounts. A provider in cooldown is never launched.
- `joined`: the container started. `agent` (provider, model, persona name), `joined_at`, `instead_of` after a switch, and `activity`: the last eight short lines of what the agent is doing, each with a timestamp.
- `answered`: the answer carries `agent` and its `activity` for diagnosis; the chat shows only the answer.
- `left`: the reply was cut off, with `reason` `timeout`, `restart`, `cancelled`, `auth`, `quota` or `error` and a sentence naming the true cause. A `role: "system"` message records it in the thread, so an unanswered question always ends in an answer or one left line. A worker that cannot sign in or is out of quota is replaced once by another ready provider; that provider is avoided until it frees up. The job keeps the worker's last twenty log lines (scrubbed, `excerpt`) for diagnosis.

Each provider answers as one persona (`forge/factory/personas.py`, `ui/app/utils/personas.ts`): Claude is Ember, Codex is Bolt, Grok is Nova.

Activity comes from the worker's bounded `docker logs` tail. Claude Code and Grok keep their stream-json transcript inside the adapter until the run ends, so their adapters print one `forge.activity` event per tool call while it runs (tool name and a few truncated inputs; never results, file contents or model text, `forge/harnesses/activity.py`). Codex `exec --json` events already reach the logs. `forge/factory/activity.py` maps both to lines such as "Reading issue #7" or "Searching the repository for `coach`": at most a program and one argument, credential-looking text dropped, every line scrubbed and capped at 100 characters. Without structured events it falls back to coarse phases.

Restart detection: each service process has a boot identity, stamped on the jobs it launches. A job from an earlier process whose container is gone, or exited without a result, leaves with `restart`; one still running is adopted. Sending the same words as the unanswered question is never a second copy: while the reply is on its way it is a no-op, after it failed it is Try again.

GET `/api/issues/discussion?issue=<key>&before=<sequence>` reads a conversation. POST `/api/issues/discussion` accepts `send` (key, message, request_id), `cancel` (key), `apply` (key, message_id, optional confirmed reason), or `undo` (key, message_id), behind existing authentication, host, origin, action-token and reset guards. Apply retains the operator identity, exact proposal revision and confirmed reason before submitting the stable message-ID request. Automatic close intent commits in the same transaction as the answer and completed discussion execution. The supervisor drains retained intents even after worker cleanup or restart. A changed revision invalidates an unsubmitted proposal; a persisted request retains its original authority.

Every linked assistant proposal projects `action_state`: request_id, status (`preparing`, `pending`, `applied`, `rejected`), message, reversible, and optional undo (request_id, status, message). The legacy receipt field identifies submission only. Actual status comes from `issue_action_requests` and `operator_action_effects`, including ordinary clicked proposals. Neither model prose nor a receipt grants success or Undo.

Undo uses another stable request in those same canonical tables, with the authenticated actor, original close request, issue version and delivery baseline pinned. Close effects record the last prior state event and independently confirm one controller-authored `closed` event from [GitHub issue events](https://docs.github.com/en/rest/issues/events). Reopening requires that exact closure still be the latest state event and confirms one subsequent controller-authored `reopened` event. Lost replies reconcile that lineage before checking authority for a new write. An observation of Forge’s own completed close or reopen may advance issue and delivery versions; acknowledging its exact effect does not rewrite those newer facts. If settlement still remains, the existing observation request completes it. Every new PATCH still requires the original delivery baseline and, for Undo, the pinned issue version. Missing event evidence remains pending; conflicting lineage rejects the request. Old close receipts without event lineage are not reversible. Legacy JSON discussions do not grant automatic close authority because they do not retain authenticated message authors.

## UI resource cache

Activity, header and detail share the factory resource; issue list queries and detail keys have separate entries. The request-scoped Nuxt app owns the cache, with no cross-request singleton data or local-storage persistence. One poll owner per key, shared requests, five-second default freshness, at most eighty idle entries and five-minute idle retention. Active subscribers are retained. Readiness middleware uses a five-second freshness window and a five-second request timeout, revalidating a known-active state in the background; explicit lifecycle invalidation forces a fresh check; server guards remain authoritative.

Actions invalidate cached generations before and after the request so an older poll cannot overwrite a newer decision. Views revalidate promptly and retain last-known data on failure. Reset and rejected authorization clear cached data. No optimistic worker-start, merge, approval or eligibility claims. Issue filters and Activity's completed-section state survive client navigation.

#49 still owns whole-page initial SSR, ETag/304 and heavy-section response work. This change does not change those backend contracts.


## Historical release verification (September 2026)

- Linux: 1,283 Python tests passed (four skips). The broad macOS run is not a passing result because legacy tests require Linux process/cgroup behavior; focused portable tests passed locally.
- UI: 272 unit tests, Vue/server typechecks and production build passed on macOS and Linux. All twelve Chromium browser suites passed locally, including conversation send/apply/reload, plain-HTTP message identity, keyboard controls and no horizontal overflow at 320/390/1440 px.
- Authenticated read-only qualification passed for Codex and Claude using the current accounts and the discussion result schema. Both containers were verified removed afterward. This qualifies reconstructed context, not native session resume.
- The deployed API-to-scheduler-to-Claude-to-UI path completed a read-only question on an existing blocked issue. Its two messages persisted; no proposal was applied. Repeated message identity was deduplicated; a missing-token/cross-origin POST returned 403.

Controlled navigation benchmark (local Chrome on the Mac development machine, identical fixtures and 750 ms injected latency on client API GETs):

| Activity measured | Pre-release installed UI artifact | New UI |
| --- | ---: | ---: |
| Reopen a previously viewed issue, through the Issues list, to next paint | 1,525 ms | 17 ms |
| Factory GETs during this specific journey | 7 | 2 |
| Issue-detail GETs during this journey | 3 | 1 |
| Issue-list GETs during this journey | 5 | 2 |

These are individual controlled runs, not production latency percentiles. Request totals include polling during the journey; the slower baseline also spends longer polling. The old artifact was tested separately without changing production. Reproduce with `FORGE_E2E_SPEC=resources.spec.mjs`; `FORGE_E2E_OUTPUT_DIR` selects a saved build and `FORGE_BENCH_BASELINE=1` records the comparison without imposing the new warm-render target. Browser executable selection follows the normal e2e runner.
