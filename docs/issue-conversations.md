# Issue conversations and responsive views

An issue's **Discuss blocker** control opens a durable conversation. The agent receives the issue, candidate identity, the four latest attempt summaries, recent decisions and twelve recent conversation messages. It has a read-only repository credential and its own disposable runner. It can explain findings and propose an available issue action. Only **Apply this decision** submits that exact proposal through the normal controller validation; discussion itself does not change eligibility or approve a merge/security exception.

## Session continuity decision (26 September 2026)

Checked the actual installed image `forge-runner:stuck-recovery` without credentials or network access:

| Provider | Installed CLI | Native capability | Existing Forge artifact | Implementation |
| --- | --- | --- | --- | --- |
| Codex | 0.153.4 | `codex exec resume SESSION_ID` | Fresh `exec` per assignment; disposable home removed on reset, no durable session mapping | New discussion with saved evidence |
| Claude | 2.1.273 | `--resume`, `--fork-session` | Adapter explicitly uses `--no-session-persistence` | New discussion with saved evidence |

CLI help establishes the flags, not successful native resumption. Native resume is deliberately **not qualified or enabled** here. Authentication and session history are different: existing ChatGPT and Claude logins are retained, but sharing a login cannot restore deleted conversations. Old issues and unavailable sessions always have the reconstructed-context route.

The latest attempt's provider is preferred if verified and available; another connected provider can answer when it is not. The UI identifies the source attempt and labels reconstructed context. No active worker conversation or worktree is modified. A future native-resume implementation would need explicit issue/attempt/session binding, selective transcript persistence excluding credentials, expiry/deletion, isolated forks and real tests across cleanup/restart. Never copy a provider home directory as a session archive or resume `--last` on a shared account.

## Lifecycle and limits

`state/issues/discussions.json` stores conversations and the currently retained job intent. One discussion worker runs at a time, after productive dispatch. It obeys existing memory admission and provider leases, uses a five-minute execution deadline and gives up queued admission after ten minutes. It never consumes issue failure attempts. Setup/reset cancels discussions before touching accounts. Cleanup failure retains the job and lease until absence is verified.

The UI polls bounded snapshots, including public assistant text emitted so far, every 2.5 seconds while open; this is incremental delivery, not token-level SSE. Private reasoning and tool input/output are excluded. Reopening restores server history; older messages paginate twenty at a time. Inputs are limited to 4,000 characters, final answers to 12,000, reasons to 2,000 and retained conversations to 1,000 messages per issue. Twelve recent messages and bounded attempt summaries form the handoff; earlier turns remain readable but may be absent from the prompt.

GET `/api/issues/discussion?issue=<key>&before=<sequence>` reads a conversation. POST `/api/issues/discussion` accepts `send` (key, message, request_id), `cancel` (key), or `apply` (key, message_id), all behind existing host/origin/action-token/reset guards. Request IDs deduplicate messages and proposal receipts across interruption. An applied receipt means submitted; the existing issue action status reports pending/applied/rejected, and the scheduler decides actual admission. A changed revision/head invalidates an unapplied proposal.

## UI resource cache

Activity, header and detail share the factory resource; issue list queries and detail keys have separate entries. The request-scoped Nuxt app owns the cache, with no cross-request singleton data or local-storage persistence. One poll owner per key, shared requests, five-second default freshness, at most eighty idle entries and five-minute idle retention. Active subscribers are retained. Readiness middleware uses a five-second cache and a five-second request timeout; server guards remain authoritative.

Actions invalidate cached generations before and after the request so an older poll cannot overwrite a newer decision. Views revalidate promptly and retain last-known data on failure. Reset and rejected authorization clear cached data. No optimistic worker-start, merge, approval or eligibility claims. Issue filters and Activity's completed-section state survive client navigation.

#49 still owns whole-page initial SSR, ETag/304 and heavy-section response work. This change does not change those backend contracts.
