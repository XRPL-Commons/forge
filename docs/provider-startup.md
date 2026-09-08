# Provider startup

Set `FORGE_CLAUDE_BIN` in the supervisor environment to an absolute executable path. Prefer the native Claude executable when available; a symlink is resolved before launching. Without this setting, the supervisor resolves `claude` from its own PATH. Each launch checks that the executable exists and is executable before creating a tmux session or charging a launch attempt. Repository shell startup cannot change the selected executable.

Failed startup is recorded as `provider_start_failed`, with a 60-second retry cooldown. Existing authentication, permission, quota and exhausted launch guards remain effective. Local `state/provider-starts/<session>.json` retains the exit status when available, tmux generation and a bounded terminal diagnostic. These private diagnostics should not be published. A normal provider exit after dispatch is recorded separately without capturing task output.

A build, review or audit shell that never received a dispatch can be recovered after 60 seconds only with two stable proofs under the supervisor lock: recorded session generation, no children, empty shell prompt, clean canonical published main, fresh eligible role work, and no startup marker or activity indicating work. Recovery preserves all failed launch charges and never discards unpublished or dirty work.
