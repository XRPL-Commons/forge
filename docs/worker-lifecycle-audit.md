# Worker lifecycle audit

The controller must distinguish an occupied session from useful execution. A
finished turn does not prove an issue is complete, and a paused provider must not
reserve scarce capacity indefinitely when another harness can continue safely.

## Invariants and repairs

- Cleanup requires the inherited exclusive supervisor lock, an exact current
  tmux generation, an empty idle prompt, no live child work, and verified role
  handoff evidence. The former unconditional empty-queue teardown is removed.
- Repository references must agree. A conflicting canonical GitHub URL cannot
  authorize a release merely because an issue or PR number matches.
- Verification has whole-operation deadlines as well as per-command timeouts.
  Unknown evidence retains work and creates an explanatory receipt.
- Dispatch markers and watchdog counters belong to one session generation.
  Partial input delivery is recorded as uncertain and is never blindly repeated.
- Actual role progress clears only the no-progress guard. Authentication,
  permission, usage and launch-budget failures retain their distinct meanings.
- Cleanup decisions enter the unified activity ledger. Repeated idle retention
  becomes actionable with its actual reason; normal processing does not create
  attention noise.
- Top-level slot metrics show occupied capacity, working, blocked, idle and
  unknown workers, with per-role reservations. These are different measurements.

## Harness routing

New worker generations alternate globally between Claude Code and Codex CLI,
regardless of role. Ongoing work can retain its current conversation. A local
routing ledger stores the last harness, one-shot operator preference, exact
session bindings and provider outage records.

An explicit provider usage-limit or authentication error pauses that provider
for approximately one hour. Other new work uses the available harness. No remote
account check runs for each dispatch. After the cooldown, one leased real work
attempt tests the provider; its exact live generation retains the lease until
an outcome. Uncertain process evidence does not authorize a second probe.

Task failures, permission errors and security findings are not proof of exhausted
account credits and do not open an hourly account-wide outage. If both providers
are unavailable, work stays queued and the dashboard shows the retry condition.
The local cooldown is not a promise about the provider's allowance reset time.

Codex uses its existing CLI account and configured model. Its structured execution
receipt contains process identities, thread identity and execution timestamps.
Private raw logs are bounded; the dashboard receives normalized observations.
A completed process is not equated with a completed issue. Background children
continue to reserve the worker and prevent competing execution.

A provider handover must prove the old generation is quiescent, preserve the
checkout and claim context, fence the old process, and only then permit another
harness to continue the same work. It must never reset dirty files or create
simultaneous writers. Stable private native executable copies prevent package
updates from removing a binary during launch.

Operator commands use the controller's configured state and repository paths:

```sh
python3 -m forge.routing --state STATE --config CONFIG status
python3 -m forge.routing --state STATE --config CONFIG next --provider codex
python3 -m forge.routing --state STATE --config CONFIG retry --provider claude
```

`next` changes the next new execution's preference; it does not interrupt active
work. `retry` explicitly permits an earlier real attempt, without probing an
account or purchasing credits. The dashboard routing interface is read-only.

## Validation approach

Unit tests cover generation fencing, interrupted dispatch, release proofs,
provider failures, cooldown/probe decisions and observation precedence. Stateful
integration tests run the real shell and Python controller against isolated fake
tmux/GitHub/git/provider boundaries. The Codex integration emits real structured
JSON from a test-owned process. A host isolation sentinel rejects unintended
access to real worker sessions. Browser checks cover desktop/mobile layout,
slot occupancy, provider labels, cooldowns and retained-work explanations.

## Limits

A running process is evidence of execution, not proof of useful progress. Forge
must not kill it solely because time has elapsed. Controller and worker files
currently share an operating-system user; this is not a strong security boundary.
Historical events are durable, but the current activity API has a bounded latest
window rather than a complete paginated history browser.

## Deployment verification (2026-09-08)

The complete source suite passed 503 tests (4 skipped), and the browser suite
passed desktop/mobile checks. Deployment occurred between supervisor passes;
all pre-existing worker identities were preserved during the file update.
The controller subsequently proved and performed the two provider handovers,
retaining their original claims and dirty worktrees. The live dashboard accurately
reported provider availability and both reserved slots.

A native CLI option conflict was discovered on the first Codex starts before any
thread or task was created. `--approve-for-me` already selects workspace-write;
passing a separate `--sandbox` option is invalid in the installed CLI. Both normal
and exact-thread resume option sets were checked locally against the native CLI,
without another provider request. Startup repair must be restricted to the
verified no-thread argument failure; a generic failed task is not retry authority.

The pinned native distribution also requires its matching `codex-code-mode-host`
companion. Both binaries are now copied with independent inodes and verified
hashes. A typed missing-host runtime error is classified as failed/blocked even
if a model subsequently emits `turn.completed`; agent prose and tool output do
not trigger this classification. After installing the companion, the operator
resumed the exact two threads under the lifecycle lock, after proving that no
tool work had occurred and all preserved file hashes still matched. Both workers
then completed real commands successfully, and the live API reported them busy
with provider `codex`. The startup/runtime repair has 27 focused passing tests.
