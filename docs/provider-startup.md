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
