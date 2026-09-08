# Local activity ledger

`forge.activity` writes private `state/activity.sqlite3` (0600). SQLite transactions serialize supervisor writes and timer samples. Events are append-only; the sessions table is a current observational projection. The dashboard reads this store without creating it. Missing data is explicitly unavailable.

Install `deploy/forge-activity.service` and `.timer` as user units. The timer samples every thirty seconds, including when the dashboard is closed. It reads tmux session metadata and at most eighty recent pane lines only for enabled configured build/review/audit sessions. It never captures unrelated sessions, sends terminal input, contacts GitHub, or sends notifications. Raw pane content and arbitrary metadata keys are never stored.

Supervisor events:

```sh
python3 -m forge.activity --state "$STATE" --config "$ROOT/repos.d" event --session "$session" --event dispatch --reason initial_task
```

Other events: `session_created`, `session_stopped`, `admission_deferred`, `paused`, `nudge`. CLI validates the enabled session allowlist; Python `record` callers must supply trusted configured identities. Every dispatch is a new correlation run. Errors exit nonzero and must be reported by callers. Admission deferrals include selected bounded fields from the capacity receipt only when the requested session matches.

`dispatched_at` is when supervisor sent the task, not proof the client accepted or started execution. `execution_started_at` remains unknown without structured execution evidence. `start_basis=supervisor_dispatch` labels the dispatch timestamp. `observed_active_since` is the first observed busy signal of the current active interval. A worker already busy when collection begins has no inferred earlier execution start. `session_started_at` is tmux session creation, not execution start.

The observer records busy, idle, shell, blocked, exited, and unknown transitions, deduplicating unchanged states. Interrupt/prompt signals are compatibility observations. Busy-to-idle never means an issue completed; run correlation persists until the next dispatch or observed session exit/recreation. Timeouts and ambiguous panes are unknown, never inferred exits. The guard file supplies observed cooldown reasons without mutating health state.

Pipeline changes preserve local counts, claimed WIP IDs, and queued IDs. These are repository snapshot observations, not proof a worker has an assignment or made progress. GitHub remains the source for lifecycle outcomes and raw PR/review/audit evidence. Polling cannot recover changes between samples, activity before installation, or exact turn starts. No ETA is inferred from this history.

`read_status(state, allowed_sessions=None)` returns observer freshness, latest session state, and latest 100 allowed events, newest first. Dashboard callers must pass their configured allowlist. The store retains events locally; no automatic destructive retention runs. Monitor file growth before introducing an explicit retention/export policy.
