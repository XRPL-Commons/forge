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

## Grok Build (optional, not yet qualified)

Grok Build (`grok`, xAI) is registered as a third harness. Routing now
round-robins over `claude`, `codex` and `grok` in that order, skipping any
harness that is unconfigured, blocked by an outage, or not yet qualified; with
two harnesses this is the existing alternation. The legacy tmux supervisor
dispatches only qualified harnesses (Claude and Codex). Grok is therefore
selectable by routing state, visible in readiness, and usable through the
bounded model-only transport in `forge.worker`, but it is never dispatched to a
repository worker until the runner qualification below passes.

### Pinned distribution

| Item | Value |
|---|---|
| Version | `1.0.34` (stable channel pointer `https://x.ai/cli/stable` on 2026-09-17; `enterprise` pointed at `1.0.35`) |
| Artifact | `https://x.ai/cli/grok-1.0.34-linux-x86_64` (also `.zst`, `.gz`; GCS fallback `https://storage.googleapis.com/grok-build-public-artifacts/cli/`) |
| SHA-256 of the decompressed ELF | `be5905e107d2b8b5f3c142d21ecfe4c8fd32a913d2fd551b788707930c4dc80d` |
| Official installer | `curl -fsSL https://x.ai/cli/install.sh \| bash -s 1.0.34` installs `~/.grok/downloads/grok-linux-x86_64` and symlinks `~/.grok/bin/grok` to it |

Snapshot the ELF with the same staging steps used for Claude, into
`<private-runtime>/providers/grok/1.0.34/<sha256>/grok`, verify `grok --version`
prints `grok 1.0.34 (3736acbc8658)` from an empty directory with a system-only
PATH, and set `FORGE_GROK_BIN` to that exact path. Grok is never discovered from
PATH: without `FORGE_GROK_BIN` it is reported as `not_configured` and is absent
from the rotation. The isolation provisioner accepts optional `--grok-source`
and `--grok-sha256` values and creates the `forge-grok` identity only then.

### Authentication on a fresh Linux VM

Grok keeps credentials in `$GROK_HOME/auth.json` (default `~/.grok`, mode
`0600`) and refreshes them in place; a running process picks up a rewritten
file without restart (hot reload). Use a dedicated `GROK_HOME` for the Forge
identity so the operator's personal login is never shared:

```
export GROK_HOME=/var/lib/forge-clients/grok/home
grok login --device-code          # prints a code and URL; complete it in any browser
grok -p "reply with ok" --no-auto-update --output-format json
```

`XAI_API_KEY` (from console.x.ai) is the non-interactive fallback and is used
only when no session token is active. Give it to the supervisor environment
only; the executor unit unsets it for repository commands. To reconnect after
an authentication outage, repeat `grok login --device-code` under the same
`GROK_HOME`, then run `python3 -m forge.routing --state STATE --config CONFIG retry --provider grok`.
`grok logout` clears the file. Forge never copies, rewrites, or migrates
`auth.json`; a stale execution therefore cannot overwrite a newer login, but two
concurrent processes sharing one `GROK_HOME` both refresh the same file, so the
Forge identity must not be shared with interactive use.

Verified unauthenticated behaviour (1.0.34): exit code `1`, stderr
`Error: Not signed in. To authenticate without a browser, run: grok login --device-code ...`,
and with `--output-format json`/`streaming-json` a stdout line
`{"type":"error","message":"Not signed in. ..."}`; with
`streaming-messages-json` a `result` line with `subtype` `error_during_execution`,
`is_error` `true` and the message in `errors`. Forge classifies this as
`blocked_auth`, an account outage that never consumes an issue's attempt budget.
Documented exit codes: `0` success, `1` error, `130` SIGINT, `143` SIGTERM.

### Headless contract used by Forge

`forge.worker` builds
`grok --prompt-file <attempt>/input.txt --tools "" --permission-mode dontAsk --no-subagents --disable-web-search --no-auto-update --output-format streaming-messages-json --json-schema <schema> [--resume <id>]`.
Headless Grok never reads stdin, so the prompt is the attempt's private
`input.txt` (mode `0600`). `streaming-messages-json` is the same wire format as
Claude's `stream-json` (`system/init`, `assistant`, `user`, terminal `result`
with `session_id`, `is_error` and `structured_output`), so the existing parser
and the same unexpected-tool rejection apply. Resume uses `-r <session_id>`
recorded from a completed result; `-s <uuid>` creates a session with a known
identity. Environment reaching the CLI is limited to `GROK_HOME` and
`XAI_API_KEY`; provider secrets of other harnesses and `GH_TOKEN` are never
inherited. Model selection is `-m <model>`; Forge does not pass one yet, so the
configured default in `$GROK_HOME/config.toml` applies.

Managed policy for the staged executor lives in `deploy/executor-policies/grok`
and is bound read-only at `/etc/grok`: `requirements.toml` pins (which user
config, environment and remote settings cannot override) `sandbox.profile =
"strict"`, `ui.disable_bypass_permissions_mode = true`, `cli.auto_update =
false` and disabled telemetry/web fetch/file writes; `managed_config.toml`
carries deny rules for the client home. The executor unit sets
`GROK_HOME=/home/forge/client`, `GROK_DISABLE_AUTOUPDATER=1`,
`GROK_TELEMETRY_ENABLED=0`, `GROK_FEEDBACK_ENABLED=0` and unsets `XAI_API_KEY`,
`GROK_DEPLOYMENT_KEY`, `GROK_AUTH_PROVIDER_COMMAND`, `GROK_CONFIG` and
`GROK_CONFIG_PATH`.

### Recorded constraints (unsupported until qualified)

- No Grok equivalent of `forge.codex_worker` exists: repository build, review,
  audit and recovery dispatch through the legacy tmux supervisor is not
  implemented. The supervisor refuses a `grok` selection with a log line, and
  routing excludes unqualified harnesses from `select`, so this cannot occur
  silently. Activity classification and provider handover treat a Grok pane as
  unknown. Qualification also requires the interrupted-publication recovery
  checks that Codex passed.
- `--tools ""` was verified to parse on 1.0.34 but not verified to remove
  every tool on an authenticated run. The worker rejects any `tool_use` block as
  `blocked_permission`, so the transport fails closed.
- Only the not-signed-in error text has been observed. Quota and rate-limit
  messages are classified by generic substrings (`usage limit`, `insufficient_quota`,
  ...) until a real exhaustion is captured. Grok's `streaming-json` `error`
  events carry only `message`, so no error code is available there.
- `--sandbox strict` needs bubblewrap user namespaces. On an Ubuntu host with
  restricted unprivileged user namespaces it fails with
  `bwrap: setting up uid map: Permission denied`; Forge classifies `bwrap:` as
  `failed_infrastructure`, never as a provider outage. The AppArmor `userns`
  profile installed by `deploy/provision_isolation.py` is the intended fix; no
  synthetic native isolation probe has been written or run for Grok yet, and
  `deploy/test_grok_isolation.py` does not exist.
- Per-harness model configuration, the Forge settings page and a device-code
  login helper are not implemented; readiness and reconnect instructions are
  exposed by `python3 -m forge.routing ... readiness` and in the dashboard
  status payload.
