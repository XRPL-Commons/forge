# Worker recovery hardening — 2026-09-06

Implemented in the staged Forge package, with no live deployment, authentication transfer, model call or merge:

- A main-thread runner receiving SIGTERM/SIGINT cancels and reaps its CLI process group before persisting a cancelled result. Callback failures also reap the group and leave an infrastructure-failure receipt.
- CLI permission failures remain policy blockers. Known bubblewrap, disk and memory setup errors are infrastructure failures, not provider-account outages.
- Result validation rejects resumed-session identity changes, conflicting session IDs, duplicate completions, missing explicit Claude success flags and unexpected tools. A model-only tool violation is a policy failure, not permission to try another provider.
- Final artifacts must be regular files without symlinks/hardlinks and are opened without blocking, with bounded reads. A FIFO cannot hang post-execution parsing beyond the worker deadline.
- Synthetic regression proves an inheritable dummy private descriptor, SSH socket and unrelated endpoint variable do not reach the CLI. The launcher retains close_fds=True and its environment allowlist.
- Provider success now requires the trusted attempt start timestamp. A success from an attempt started before the latest account failure cannot close that circuit.

Validation: 17 worker tests and 6 provider tests pass. Previously captured native Codex/Claude initial/resume streams all still validate. These replays make no provider calls.

## Minimal remaining live-executor design

Keep WorkerRunner's live-mode refusal. Introduce a separate trusted launcher after the following configuration is concretely installed and tested:

1. A controller creates a unique systemd service for each immutable attempt generation, running the pinned provider executable as its dedicated identity. The controller owns the unit name and attempt record; a worker cannot select a different UID, service, executable, policy or workspace.
2. The unit uses KillMode=control-group, an execution deadline and aggregate resource limits. Cancellation revokes generation authority first, stops the unit, and confirms its cgroup has no remaining processes before releasing capacity or launching a replacement. SIGKILL, a killed Python runner and descendants calling setsid require this boundary; process-group tests do not establish it.
3. A private mount namespace exposes root-owned per-provider policy at the native managed-policy location. For Codex this includes /etc/codex/requirements.toml; for Claude it includes the managed settings file. Use per-service read-only bind mounts so Forge's restrictions do not unexpectedly alter the operator's personal sessions. Pin the effective executable version and policy digest in the controller receipt, and prove lower configuration and CLI flags cannot widen them.
4. The provider client keeps only its scoped model authentication. Repository commands receive no authentication files, provider environment secrets, GitHub token, SSH agent or inherited descriptor. Claude initially exposes Bash only with native sandbox and subprocess scrub forced on; Codex uses the tested restricted-read profile plus managed direct-file denies and disabled unrelated tool surfaces. Real protected-auth canaries remain necessary: the earlier native probes used only dummy secrets.
5. Task publication and merge stay outside the client context. Trusted code verifies artifact hashes and exact candidate/generation, then publishes or merges through its separate credential. Worker text, labels or a boolean claiming isolation never authorizes those operations.

Acceptance must cover runner SIGKILL, setsid/double-fork descendants, auth-file/environment/proc/descriptor access, nested client attempts, policy edits, conflicting generation callbacks, quota recovery and exact-head mutation. The synthetic runtime remains useful for bounded model-only analysis while these gates are completed.
