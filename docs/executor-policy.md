# Dummy executor policies — staged

These files are candidate policies for a private attempt namespace. They do not authorize model access, repository execution or credential transfer. `launcher-contract.json` deliberately keeps `provider_activation=false`; the current executor can run only its fixed synthetic Python fixture. No system-wide settings are installed by this change.

## Mount and launch contract

A trusted controller creates `/var/lib/forge-executor/attempts/<canonical UUID>` with a root-owned parent and policy source, plus provider-owned `workspace/` and `client/`. Bind these at `/home/forge/workspace` and `/home/forge/client`. The only credential fixture is `/home/forge/client/dummy-credential`, containing synthetic bytes. Bind the selected **whole policy directory**, read-only, at `/etc/codex` or `/etc/claude-code`; this excludes ambient sibling managed files. For Codex additionally bind `codex/config.toml` read-only at `/home/forge/client/config.toml`. Preserve the pinned `/opt/forge/bin/codex` and `/opt/forge/bin/claude` paths.

Policy sources, every ancestor, the launcher, and executable must be protected before Python imports or provider startup. Revalidate their types, ownership, permissions and identity before each attempt; bind from trusted sources. A caller-supplied path or `isolated=true` field is not an attestation. Bind the receipt to attempt UUID, provider binary hash/version, policy hashes, service invocation ID, mount sources, task revision, and cgroup. Policy files missing inside the service are a failure, never a skipped check.

The trusted system-service launcher must close inherited descriptors, construct a minimal environment, clear supplementary groups, prohibit privileged sockets and host credentials, impose deadline/resource limits, and kill the entire cgroup on cancellation. A shared provider UID is not isolation between concurrent attempts: serialize that UID until a demonstrated PID/user boundary or unique per-attempt identity prevents peer inspection and signaling. `ProtectProc` alone does not separate same-UID clients.

## Codex candidate

The only selectable managed profile is `forge_dummy_executor`. It extends read-only, denies root reads, allows minimal runtime and the pinned helper, and grants a fixed workspace write. It intentionally does not use dynamic `:workspace_roots`, so adding a directory cannot expand the write grant. Commands have no network or approval escalation. The client config disables environment inheritance; managed controls additionally close MCP, plugins, browser, apps, hooks and agent spawning. Credential deny-read rules also cover direct filesystem tools.

[Official OpenAI managed configuration](https://learn.chatgpt.com/docs/enterprise/managed-configuration) documents `/etc/codex/requirements.toml`, managed profile/feature constraints and higher-priority cloud requirements. The [configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference) documents enforced `permissions.filesystem.deny_read`. The eventual authenticated client's effective policy must match the receipt: do not assume the local system layer wins over its organization layer.

## Claude candidate

Only Bash is exposed by the immutable invocation. Built-in file tools, web tools, subagents and MCP are removed or denied. Native sandboxing is mandatory; unavailable sandbox, unsandboxed retry and weaker nested mode are prohibited. Client paths and credential variables are denied. The launcher sets `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1`; ordinary user/project settings, plugins, hooks and slash commands cannot supply executable customization in this invocation.

[Claude managed settings](https://code.claude.com/docs/en/managed-settings) documents the Linux directory and source precedence. Remote settings can supersede local policy; effective authenticated settings are therefore an activation gate. [Claude sandboxing](https://code.claude.com/docs/en/sandboxing) applies to Bash, not every tool. [Credential scrubbing](https://code.claude.com/docs/en/env-vars) supplements filesystem restrictions. This candidate does not claim a default-deny read filesystem outside the explicitly protected paths; the outer mount namespace must hide unrelated host data.

`--bare` is chosen for dummy clients: it excludes ambient startup integration and accepts API-key authentication rather than the operator's OAuth store. Removing `--bare` to use subscription login would change the startup and configuration surface and requires a separate acceptance test. Nothing here transfers or rewrites existing logins.

## Evidence and remaining tests

`python3 -m unittest tests.test_executor_policy -q` passes six offline contract regressions. An isolated temporary Codex home with the candidate profile and feature table passed `codex features list` on 0.153.2; `state/executor-policy-parser.json` records the result. This validates ordinary-config syntax and recognized feature switches, **not managed source precedence**. The profile is defined only in requirements for deployment: copying it into both requirements and client config can cause duplicate-name rejection.

Earlier native Codex and fake-endpoint Claude fixtures demonstrated useful secret/network denials and unavailable-sandbox failure. Those fixtures used different paths and settings delivery. Their success does not certify this policy mount or executor. A fixed Python executor fixture establishes only the outer service boundary, not native CLI tool enforcement.

Before enabling either CLI, run inside the final service using these exact mounts and pinned binaries: (1) inspect effective managed policy and reject conflicting command-line/project/user overrides; (2) prove workspace edits while denying dummy secrets through direct reads, symlinks, hardlinks, proc aliases, inherited descriptors/environment and nested clients; (3) prove loopback/external/Unix-socket denials for commands; (4) prove children cannot inspect or signal the client or another attempt; (5) test unavailable sandbox and policy files; (6) cancel after descendants detach and verify the cgroup empties. First use a local synthetic API with all external traffic disabled. Admission of protected real authentication remains a subsequent explicit gate with effective policy rechecked; no dummy test proves unknown client-created credential descriptors are safe.
