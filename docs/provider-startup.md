# Provider startup

Set `FORGE_CLAUDE_BIN` in the supervisor environment to an absolute executable path. Prefer the native Claude executable when available; a symlink is resolved before launching. Without this setting, the supervisor resolves `claude` from its own PATH. Each launch checks that the executable exists and is executable before creating a tmux session or charging a launch attempt. Repository shell startup cannot change the selected executable.


For a native standalone executable, prefer a Forge-owned snapshot outside the package manager's installation directory. An absolute path inside an npm package can briefly disappear while npm replaces that package, even when the installed version is unchanged.

Stage upgrades before changing the service configuration:

1. Confirm the candidate is the native executable, rather than a script that depends on adjacent package files. Record its version, file identity, size and modification times, and compute its SHA-256.
2. Copy it into a private staging directory on the same filesystem as the final provider directory. Use an independent copy, not a symlink or hardlink to the package-managed file. Flush the copy to disk and make it executable without write permission.
3. Check that the source identity and timestamps did not change during copying. Require matching SHA-256 values for the original source, copied file and freshly reread source. Discard the candidate if any check fails.
4. Run the copied executable's `--version` from an empty working directory with a system-only PATH. Require the expected version, then atomically rename the validated file into a version-and-checksum path such as `<private-runtime>/providers/claude/<version>/<sha256>/claude.exe`. Never overwrite an existing checksum path. Retain a private receipt containing the source metadata, hash, destination and validation results.
5. Back up the supervisor's service configuration. During the normal safe deployment window, point `FORGE_CLAUDE_BIN` at that exact snapshot path and restart the supervisor. Verify a real dispatch after promotion. Keep the previous snapshot for rollback; do not remove binaries used by existing workers.

The version check establishes that the copied binary starts independently of npm's directory and PATH. It does not validate authentication or every execution path: normal system libraries and external tools still need to be installed, and a real dispatch verifies the integration. Keep these third-party executable snapshots and private receipts outside the public source repository.

Failed startup is recorded as `provider_start_failed`, with a 60-second retry cooldown. Existing authentication, permission, quota and exhausted launch guards remain effective. Local `state/provider-starts/<session>.json` retains the exit status when available, tmux generation and a bounded terminal diagnostic. These private diagnostics should not be published. A normal provider exit after dispatch is recorded separately without capturing task output.

A build, review or audit shell that never received a dispatch can be recovered after 60 seconds only with two stable proofs under the supervisor lock: recorded session generation, no children, empty shell prompt, clean canonical published main, fresh eligible role work, and no startup marker or activity indicating work. Recovery preserves all failed launch charges and never discards unpublished or dirty work.


Worker input and recovery are tied to the current tmux generation. The supervisor submits a task only at a verified empty provider prompt, records a submitted dispatch only after both literal input and Enter succeed, and writes a generation-specific `.up` receipt. Submission is not evidence that execution began; the activity observer supplies that separately. An uncertain send produces a private `dispatch-uncertain` receipt and stops automatic resubmission for that generation so pending input is preserved.

Unknown terminal state, capture failures and typed input never authorize a nudge or cleanup. Cleanup requires the role-specific published-work and process proof even when the entire repository queue is empty. Watchdog nudge counts reset for a new generation, observed activity, or a changed fresh role-work fingerprint. Only successfully submitted nudges consume that budget. Actual activity or authoritative progress can clear a `no_progress` hold; authentication, model usage, permission, account and launch-budget holds remain intact.

Terminal quota/authentication observations are session-scoped because copied task or tool output cannot establish an account-wide outage. The latest observed model-usage banner is recognized even when Claude returns to its ordinary input footer after the error. Quoted banners and errors preceding a later meaningful response are excluded. Reobserving the same error from the same dispatch does not extend its cooldown indefinitely. Existing independently established account guards are preserved.


## Both harnesses

Configure `FORGE_CODEX_BIN` as an absolute executable path alongside `FORGE_CLAUDE_BIN`. New worker generations use the local global routing decision, alternating Claude and Codex when both are available. A verified usage or authentication outage makes that harness unavailable for an hour; the router permits one controlled recovery attempt after that delay. Routing checks local state and executable availability, without making a provider request for every queued role. Existing generations keep their actual harness and identity.

Codex runs through `forge.codex_worker` as a bounded `codex exec --json` process inside the role's tmux shell. Private structured receipts distinguish running, completed execution, provider failure and unknown state. A successful execution does not establish that an issue or PR is complete. Reconciliation still requires current queue and publication evidence. Resume uses the exact recorded thread ID only after a completed execution, changed fresh role work, and a verified empty shell with no remaining child work; unchanged work never triggers a blind repeated command.

Switching an existing blocked worker requires a verified provider handover. Its private manifest preserves the generation, claim context, branch and changed files before retirement. The replacement reads that manifest and resumes still-active work without resetting or discarding local changes. Normal completion and handover remain separate decisions.

For Codex CLI 0.153.4, pin the matching `codex` and `codex-code-mode-host`
executables together as siblings, verify their hashes, and preserve executable
permissions. Copying only `codex` can start a model turn while leaving it unable
to execute any tools. The runner uses `--approve-for-me`, which already selects
the workspace-write sandbox; do not combine it with a separate `--sandbox`
option. A version or no-tools response alone does not verify the tool runtime:
validate startup arguments locally and verify a real authorized tool operation.

An exact typed missing-host error is a runtime failure, even if the model closes
its turn successfully. This remains distinct from a provider allowance failure
and does not open a provider-credit cooldown.
