# Isolated observer rollout — staged, no deployment performed

The observer is a detective control. It cannot establish safety for a merge, grant a merge, or replace the auditor. Deployment remains gated until worker identities cannot escalate to root or modify trusted service code, state, or credentials. A new observer account on a host where workers retain passwordless sudo or the Docker socket is not an isolation boundary.

## Exact target layout and identities

| Component | Target | Owner/access |
| --- | --- | --- |
| Reviewed Python modules, observer launcher and configuration | `/opt/forge-observer` | root-owned; observer read/execute; worker cannot modify ancestor directories |
| Observer home, pinned Claude client configuration and model credential | `/var/lib/forge-observer/home` | dedicated `forge-observer` UID; mode 0700; no worker mounts or shared SSH agent |
| Independent bare repositories | `/var/lib/forge-observer/mirrors/OWNER--REPO.git` | observer UID; directories 0700, files 0600; never alternates, shallow clones, shared objects or worker checkouts |
| Baselines, checkpoints, complete/incomplete receipts and coverage summary | `/var/lib/forge-observer/state` | observer UID; mode 0700; durable storage, backup outside worker authority |
| Notification outbox | `/var/lib/forge-observer/notifications` | observer enqueue; separate trusted notifier consumes; no worker write access |
| Dashboard export | separate allowlisted summary snapshot | trusted exporter publishes only coverage/status, never raw model credentials or arbitrary terminal panes |

The observer UID must be non-root and absent from `sudo`, `wheel`, `docker`, and `lxd`. No sudoers exception, ambient capabilities, host socket, worker home, or worker SSH agent may be accessible. Configure `FORGE_OBSERVER_WORKER_UIDS` as the complete comma-separated list of actual worker UIDs; startup rejects missing configuration and a shared identity. This list is root-controlled deployment configuration, not evidence supplied by an agent.

Use `FORGE_HOME=/opt/forge-observer`, `FORGE_OBSERVER_STATE=/var/lib/forge-observer/state`, and `FORGE_NOTIFY_STATE=/var/lib/forge-observer/notifications`. Set the service working directory to the reviewed source directory. Set `FORGE_OBSERVER_CLAUDE_BIN=/opt/forge-cli/claude/REVIEWED_VERSION/claude` to the actual installed executable, with no symlink components. The executable and all ancestor directories must be root-owned and protected from group/world writes. Runtime refuses missing/relative pins, user-home or PATH discovery, non-root executables, and replaceable ancestors. Record the exact binary version/digest during deployment; keep the service PATH restricted to root-provisioned tools so any script interpreter also resolves to reviewed code. Apply a 0077 umask and aggregate resource limits. Grant no write access outside the observer home, mirrors, state and outbox; reserve systemd policy changes to maintainers. Before Python imports any module, deployment must recursively verify the complete installed module/configuration trees and mirror contents, their owners, modes, ACLs, symlinks, and relevant interpreter/library paths. Post-import checks of selected paths cannot establish the integrity of code that has already executed; the runtime checks supplement this deployment precondition. They also do not prove token scope, Linux capabilities, ACLs, cloud policy or the absence of root-equivalent worker privileges.

## Read-only GitHub authentication and mirror setup

Provision a repository-scoped **read-only deploy key** per private mirror (or a separately verified contents-read GitHub App credential through an equally constrained adapter). Do not reuse the builder, auditor, operator or administrator credential. The current implemented transport uses ordinary SSH identity files in the observer's private home, no shared agent and no global Git config. An HTTPS private-repository credential adapter is not implemented by this transport.

For each mirror, an operator must independently match the configured `OWNER/REPO`, the immutable GitHub repository identity and the read-only key installation. Create a fresh bare repository under the target mirrors directory with a minimal local Git configuration and exact origin `git@github.com:OWNER/REPO.git`. Do not copy any worker clone's config, hooks, object alternates or remotes. Copying the old builder repository defeats the independent source boundary.

The runtime accepts only a minimal bare-repository Git configuration and the configured GitHub remote; unexpected local customization, symlink paths, group/world-writable paths, object alternates and shallow history are refused. Git invocations disable hooks, external diff/textconv, submodule recursion, ambient global/system Git configuration and inherited SSH agents. Fetch writes exactly `+refs/heads/main:refs/remotes/origin/main` and never pushes. SSH identities must be selected in the root-protected observer SSH configuration; known-host verification remains enabled.

Root-reviewed configuration lives in its own tree. In `/opt/forge-observer/repos.d/OWNER--REPO.repo`, declare:

```
REPO="OWNER/REPO"
ENABLED=true
OBSERVER_MIRROR="/var/lib/forge-observer/mirrors/OWNER--REPO.git"
```

Keep repositories explicitly disabled until their mirror/authentication checks pass. Duplicated repository identities or shared mirrors are rejected.

## Coverage activation and recovery

1. Archive existing live observer state and its known limitations without treating its old checkpoint as a verified scan receipt. Copying the old `.sha` alone cannot establish complete coverage.
2. Independently verify the mirror remote and read-only credential, then fetch under the isolated identity. Confirm the expected current main SHA against GitHub through the trusted controller. A dry run is read-only and does **not** fetch; it only inspects the already-fetched snapshot.
3. Choose the initial coverage boundary explicitly. The baseline command declares **coverage after that SHA**, so choosing current main does not audit older history. Historical backfill is a separate required exercise when prior coverage matters; do not claim it happened during onboarding.
4. Once the maintainer has approved that boundary and its exclusions, execute the baseline declaration under the isolated identity with a concrete reason:

```
python3 -m observer.run --baseline-repo OWNER/REPO \
  --baseline-sha EXPECTED_CURRENT_MAIN_SHA \
  --baseline-reason "Maintainer-approved coverage start; previous history tracked separately"
```

5. The command verifies the protected mirror and exact fetched head. A pre-existing checkpoint or baseline requires an additional explicit `--replace-baseline`; ordinary scans never reseed. Replacing a baseline after state loss or force-push is a coverage decision, not an automatic recovery action. Restore and verify the original protected state first when possible. Preserve the old epoch and receipts in the backup before replacement.
6. Start with one repository. Each completed or incomplete commit scan records repository/mirror identity, baseline epoch, observer policy version, patch hash/size, outcome, confidence and cited findings before notification or checkpoint advancement. A checkpoint beyond the baseline is accepted only with a complete receipt from that same epoch. Missing/corrupt state fails closed, including when the checkpoint equals current main.
7. Authentication failure, malformed evidence, unsupported binary/submodule changes, oversized patches, and history rewrites preserve pending coverage and produce degraded status. Fix the underlying cause and rescan from the last verified checkpoint; never skip to HEAD to clear the dashboard. Larger/binary/manual review support remains an explicit extension, not an automatic all-clear.
8. Enable the separate notifier only after its credential and outbox access are verified. Enqueue is durable and immediate; network delivery has its own deadlines and retry budget. A relay acceptance is not confirmation that a human received the message.

## Acceptance gates before production activation

- A deliberately hostile worker cannot read observer credentials, alter mirror configuration/state, replace the reviewed modules, enter the observer account, or gain root.
- The observer key can fetch the configured private repository but cannot push. Verify scope from trusted GitHub configuration; do not test by making a real push.
- A wrong remote, executable Git config, group-writable mirror, shared objects, shallow history, shared worker UID, or missing deployment declaration is refused before fetching.
- An existing checkpoint without matching baseline/receipt cannot report up-to-date. Interrupted receipt/checkpoint writes retain the old verified boundary or a visible gap.
- A finding after 12,000 characters is inspected; missing/forged file-line citations and unavailable models remain incomplete. A merge diff is checked against its first parent.
- A dry run changes no refs, state, counters, alerts or outbox. A degraded scheduled run returns a nonzero status and suppresses an all-clear heartbeat.
- Restore a protected backup into an isolated temporary location and verify the **full receipt epoch/history**, not merely the endpoint receipt. Enumerate every first-parent commit between the declared baseline and checkpoint from the independently verified mirror, and require a complete, matching repository/mirror/policy/epoch receipt for each commit; recompute patch hashes and investigate every missing or mismatched record. The current runtime validates the checkpoint endpoint receipt, not an unbroken receipt chain for all prior commits. Preserve the complete baseline, receipts, and checkpoint together; an endpoint receipt alone is insufficient evidence of historical coverage. Demonstrate pending scans resume without moving the live baseline.

No accounts, keys, mirrors, service units, GitHub objects, or live baselines were modified by this hardening task. Only offline temporary repositories and mock model transports were used for validation.
