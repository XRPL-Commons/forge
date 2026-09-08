# Observer (staged; independent mirror required)

The observer reports merged-code findings without editing repositories. It must run under a dedicated read-only identity with protected observer state and a separately provisioned mirror. Merely choosing another path does not enforce that host boundary. Each enabled observer-only `.repo` configuration needs `OBSERVER_MIRROR` pointing at that protected bare mirror. Keep these declarations in a separate root-owned configuration tree; the legacy supervisor parser does not accept observer-only keys. Using `CLONE_BUILD` as the mirror is rejected.

State defaults to `FORGE_HOME/state/observer`; `FORGE_OBSERVER_STATE` overrides it. The default Forge home is the installed source parent, never the old live tree implicitly.

Missing or empty checkpoints return `baseline_required`, never an automatic all-clear. After independently verifying mirror identity and selecting the coverage start, explicitly declare a baseline with:

```
python3 -m observer.run --baseline-repo OWNER/REPO --baseline-sha EXPECTED_CURRENT_MAIN_SHA \
  --baseline-reason "Maintainer-approved coverage start; historical coverage tracked separately"
```

This records that coverage begins **after** that SHA. It fetches and compares current main before writing. An existing checkpoint or baseline additionally requires `--replace-baseline`; never use this to conceal pending coverage. Restore verified state first when possible. Ordinary scans never silently rebaseline. Service execution holds a singleton scan lock.

Scans enumerate first-parent main history and compare merge commits to their first parent. Missing models, malformed results, patches over 100,000 characters, binary patches, and submodule changes remain incomplete. Smaller text patches are passed in full, including content beyond the former 12,000-character cutoff. Extended/binary audit remains a manual or future adapter capability; large changes are deliberately parked. Claude is invoked from the observer source directory with `--safe-mode --restricted`, built-in tools disabled, empty strict MCP configuration, and settings sources disabled. Model findings require a file, positive line number, old/new side, explanation, and exact evidence text verified against the displayed diff. Missing or forged citations become incomplete coverage. Header-looking source content cannot replace a diff citation path. Deployment must also protect its environment, credentials, and working directory.

Heuristic findings can raise suspicion during model failure, but cannot establish completed coverage. A failed or incomplete scan retains its previous checkpoint; successful preceding commits may advance only to their last fully inspected SHA. Checkpoints use atomic replacement and fsync. A repository/mirror/policy-bound baseline and complete per-commit receipt are required before an existing checkpoint can count as covered. Every scan persists its receipt before notification or advancement; legacy `.sha` files alone are insufficient. Runtime verifies the endpoint receipt, not the full preceding receipt chain; restore acceptance must independently verify every receipt in the coverage epoch. `coverage.json` records per-repository statuses; incomplete runs suppress the daily all-clear. Findings are persisted before enqueue, and stable event keys prevent repeated outage notifications. Network delivery is owned by `forge.notifications`, not the scanner.

Startup requires a dedicated non-root identity outside the declared worker UIDs and host-administration groups, root-owned code, and protected paths. `FORGE_OBSERVER_WORKER_UIDS` and `FORGE_OBSERVER_CLAUDE_BIN` must be explicitly configured. The latter must name an absolute root-owned executable under root-owned protected ancestors; ambient PATH/home discovery is disabled. Deployment must verify the complete code/configuration/mirror trees before Python starts: selected post-import checks do not establish code integrity. Git fetch verifies a minimal independent bare mirror and exact remote, ignores ambient Git configuration/SSH agents, and pins the main refspec. Dry-run does not fetch or alter counters/state; degraded runs exit nonzero.

See [the exact staged rollout and acceptance gates](../docs/observer-rollout.md). No live observer scan or baseline was run during implementation. Offline validation:

```
python3 -m unittest discover -s observer/tests -q
python3 -m unittest discover -s tests -p test_notifications.py -q
```
