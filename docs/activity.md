# Local activity ledger

`forge.activity` writes private `state/activity.sqlite3` (0600). SQLite transactions serialize supervisor writes and timer samples. Events are append-only; the sessions table is a current observational projection. The dashboard reads this store without creating it. Missing data is explicitly unavailable.

Install `deploy/forge-activity.service` and `.timer` as user units. The timer samples every thirty seconds, including when the dashboard is closed. It reads tmux session metadata and at most eighty recent pane lines only for enabled configured build/review/audit sessions. It never captures unrelated sessions, sends terminal input, contacts GitHub, or sends notifications. Raw pane content and arbitrary metadata keys are never stored.

Supervisor events:

```sh
python3 -m forge.activity --state "$STATE" --config "$ROOT/repos.d" event --session "$session" --event dispatch --reason initial_task
```

Other events: `session_created`, `session_stopped`, `admission_deferred`, `paused`, `nudge`. CLI validates the enabled session allowlist; Python `record` callers must supply trusted configured identities. Every dispatch is a new correlation run. Errors exit nonzero and must be reported by callers. Admission deferrals include selected bounded fields from the capacity receipt only when the requested session matches.

`dispatched_at` is when supervisor sent the task, not proof the client accepted or started execution. `execution_started_at` remains unknown without structured execution evidence. For Codex it records the verified execution runner start, not proof of the first model token. `start_basis=supervisor_dispatch` labels the dispatch timestamp. `observed_active_since` is the first observed busy signal of the current active interval. A worker already busy when collection begins has no inferred earlier execution start. `session_started_at` is tmux session creation, not execution start.

The observer records busy, idle, shell, blocked, exited, and unknown transitions, deduplicating unchanged states. Interrupt/prompt signals are compatibility observations. Busy-to-idle never means an issue completed; run correlation persists until the next dispatch or observed session exit/recreation. Timeouts and ambiguous panes are unknown, never inferred exits. The guard file supplies observed cooldown reasons without mutating health state.

Pipeline changes preserve local counts, claimed WIP IDs, and queued IDs. These are repository snapshot observations, not proof a worker has an assignment or made progress. GitHub remains the source for lifecycle outcomes and raw PR/review/audit evidence. Polling cannot recover changes between samples, activity before installation, or exact turn starts. No ETA is inferred from this history.

`read_status(state, allowed_sessions=None)` returns observer freshness, latest session state, and latest 100 allowed events, newest first. Dashboard callers must pass their configured allowlist. The store retains events locally; no automatic destructive retention runs. Monitor file growth before introducing an explicit retention/export policy.

Generation tracking uses the exact tmux `generation_id` (`$session_id|created_epoch|%pane_id|pane_pid`) and `session_started_epoch` alongside the readable creation time. An announced replacement clears the previous generation before dispatch, so its first observation preserves the new dispatch timestamp. Unannounced replacement invalidates the old run. Observations acquired before a newer lifecycle event, or belonging to the immediately previous exact generation, cannot overwrite the current projection.

The supervisor and observer share `python3 -m forge.activity classify --command claude --state-only`, reading a bounded ANSI-preserving pane on stdin. It returns `busy`, `idle`, `shell`, or `unknown`; unknown is a successful classification, not permission to send input. Idle requires the same empty-input or faint-suggestion proof as cleanup, including the idle footer. A caller whose capture fails must report unknown instead of classifying missing input.

`lifecycle_decision` records cleanup decisions as history without changing worker activity, dispatch, or observed state. Its bounded metadata includes `phase`, `outcome`, `generation_id`, `since`, and `reason`; a failed cleanup proof is not evidence that the worker advanced.

Codex activity comes from `codex_worker.observe`, which matches the exact tmux generation and inspects recorded process birth identities. No terminal text is used to classify Codex. Claude retains the canonical ANSI prompt/interrupt compatibility observer. Provider run/thread identifiers are separate from supervisor dispatch correlation. Unknown or replaced receipts cannot supply an actual-provider label.
