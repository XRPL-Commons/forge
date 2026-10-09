# Isolation status

The supported factory uses disposable Docker workers with native tool sandboxing,
a runner-specific seccomp/AppArmor policy, and an internal egress network. These
are the **live** mechanisms described in [the runtime guide](factory-runtime.md)
and the [security boundary table](../SECURITY.md#runtime-trust-boundaries).
`safe-build` and memory admission are live resource controls, not credential
isolation. The tmux fleet supervisor is retired. The independent observer remains
deferred until core qualification is complete (#111).

## Staged native-account alternative

Everything below describes a **staged, inactive alternative**. Its accounts,
executable profiles and canaries are not the factory's live execution path.
Provisioning them does not change the container runner. The live SQLite execution
adapters are controller code, not this proposed host executor.

Forge's native CLI isolation is a separate rollout gate. `deploy/provision_isolation.py` prepares identities and executable provenance; it does **not** establish that repository commands cannot access client credentials.

The provisioner defaults to a dry run. It creates neither accounts nor files outside the workspace until `--apply` is explicitly supplied. It never copies authentication, starts workers, changes the operator's groups or Codex configuration, disables AppArmor, or changes global user-namespace sysctls.

The proposed operator-managed layout is:

| Resource | Ownership and purpose |
|---|---|
| `forge-codex`, `forge-claude` | Separate system accounts, `nologin`, their own primary group, no supplementary groups |
| `/var/lib/forge-clients/{codex,claude}` | Provider-owned private homes, mode `0700` |
| `/opt/forge/bin/{codex,claude}` | Native ELF copies, `root:forge-<provider>`, mode `0750`; no user-controlled symlinks |
| `/etc/apparmor.d/forge-{codex,claude}-client` | Root-owned profiles attached only to the pinned executable path |
| `/etc/forge/isolation/manifest.json` | Root-owned source paths and SHA256 pins; no credentials |

The AppArmor profiles use `flags=(unconfined)` plus `userns,`. Ubuntu documents this mechanism for allowing selected applications to create their own user-namespace sandbox without disabling the host restriction. This profile is **not an application confinement policy**: the CLI must still enforce its native filesystem, network, and subprocess-credential rules. The group-only executable permissions restrict which identities can invoke the profiled binaries. [Ubuntu user-namespace restrictions](https://ubuntu.com/blog/ubuntu-23-10-restricted-unprivileged-user-namespaces)

Review and apply in this order:

1. Run `python3 deploy/provision_isolation.py` as the operator. Inspect the source paths, hashes, account layout, and exact profile text. Codex's source must be the native ELF payload inside its installed package, not `codex.js`; Claude's installed native ELF is copied directly. This records installed provenance, not independent vendor-signature verification.
2. If the plan reports missing `bubblewrap` or `socat`, use the separate `sudo /usr/bin/python3 deploy/provision_isolation.py --install-packages` step. It invokes distro `apt-get` only. It does not change sandbox policy or install CLI software. Forge already had both prerequisites when this plan was prepared.
3. Review the script, then apply with explicit `--codex-source`, `--codex-sha256`, `--claude-source`, and `--claude-sha256` values from the reviewed dry run. Root's PATH need not discover the operator's installation. SHA256 pins are mandatory on apply, and the staged bytes are verified again before atomic publication. A changed existing executable, profile, account, or directory causes refusal; upgrades require their own reviewed migration.
4. Inspect the root-owned manifest and permissions. Run `--version` only as each dedicated identity, never as root. Confirm no additional group memberships, sudo grants, Docker access, or host sockets have been granted. Check loaded AppArmor profiles. Do not copy real authentication yet.
5. Prove native sandbox behavior with synthetic fixtures under a dedicated workspace. A dummy provider credential must be readable by the client identity outside its command sandbox but unreadable through every enabled repository tool, including shell, file-read, search, child processes, absolute paths, `/proc`, symlinks, and inherited environment variables. Also test deny-write to policies/other workspaces, network/Unix-socket denial, no unsandboxed fallback, failure when sandbox setup is unavailable, and ordinary fixture edits/tests succeeding. Keep all test values artificial.
6. Only after the dummy-file boundary passes should a separate reviewed step install narrowly scoped authentication and immutable managed policy. Bind each role's workspace to that policy; protect managed settings from repository overrides. Never use global `/etc/codex` settings casually: they could affect the operator's interactive Codex. The exact per-identity managed configuration mechanism remains an acceptance gate.

The identities also do not isolate different roles from each other within one provider: a Codex builder and Codex auditor sharing this Unix identity can access each other's files unless a separate enforced sandbox boundary prevents it. Production admission must prove that boundary or use separate role/attempt identities. Do not add GitHub merge credentials, sudo rules, Docker membership, or a general-purpose privileged broker to make the tests pass.

Provisioning is additive and stops on errors. It is not a transaction: an interrupted apply can leave a partial set of unused accounts/files. Re-run only after inspecting the root-owned targets; identical installed bytes are accepted. No existing service is restarted. For rollback, first stop any processes using these identities, reconcile work, then remove only the specifically named profiles/executables/accounts after preserving any needed data. Do not restore a global sysctl workaround or weaken another user's policy.

Validate the provisioned layout on your own host using `deploy/test_codex_isolation.py` and `deploy/test_claude_isolation.py` with synthetic credentials only. Keep inherited file descriptors closed: filesystem restrictions cannot retract access through an already-open descriptor. A client exit code alone does not prove that its tools ran successfully or that an isolation boundary held. Immutable production policy, authenticated tool execution, cross-attempt isolation and complete process lifecycle enforcement remain separate gates.
