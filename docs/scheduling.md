# Priority and execution ordering

Forge separates proposal generation from deterministic GitHub writes and builder admission. Public service templates use local rules by default. Model triage can send issue titles, bodies, labels and referenced context to a model provider. Enable it only after reviewing that data flow and explicitly setting `FORGE_LLM_TRIAGE_ALLOWED=1`, `FORGE_TRIAGE_MODE=llm`, and the selected client path in your service configuration. Missing or conflicting ownership labels block triage and admission.

Every four minutes, the triage timer checks ready issues. A model is called only
when the relevant content or triage policy changes. Hold and wip issues are never
triage mutation targets. Referenced open issues may be supplied as read-only
context. Claude runs in a fresh temporary directory with tools, MCP servers,
repository settings and slash commands disabled. Its structured output must cover
the supplied issues exactly once and pass deterministic validation. Model output
cannot issue GitHub commands, claim work, merge or deploy. Client authentication
still runs as the operator in this compatibility release; tool disabling is not a complete
host isolation boundary.

The core marks missing priorities p0 (urgent) through p3 (lowest), preserving
existing operator priorities. Its own labels also carry `priority:auto`. Changing
or removing an automatic priority hands control to the operator and latches that
choice. `priority:manual` explicitly preserves an operator choice;
`priority:retriage` hands it back to triage unless `priority:manual` is present.
Conflicting or unsupported priority labels block admission.

Dependencies are native GitHub issue dependencies, visible on the issue. The
core retains recognized explicit prerequisites even if the model omits them. New
edges require exact, explicit prerequisite evidence in the issue body; shared
files, coordination or architectural similarity are insufficient. The controller
rejects cycles and preserves existing edges. An operator-removed edge is not
silently recreated. Referenced prerequisites include same-repository issue URLs and closed issues.
Closed prerequisites satisfy an edge only when completed;
closed-as-not-planned dependencies require resolution. Cross-repository open
prerequisites block without being claimed by this repository's builder.

For a new claim, the builder must run:

```sh
~/ops/forge-watchdog/bin/forge next --repo OWNER/REPO
```

The core orders prerequisites before dependents, propagates urgency to unresolved
prerequisites, then uses oldest observed ready time and issue number for ties.
Effective urgency does not rewrite a prerequisite's visible priority. It admits
only ready, unheld, unclaimed issues with completed prerequisites and an up-to-date
triage receipt. An existing wip claim reserves the repository until resolved;
running work is never preempted. An empty result or failure means wait. Returned
PR recovery remains ahead of new claims.

The dashboard exposes the shared scheduling snapshot, blockers, effective
priorities and triage reasons. GitHub holds operator-visible priorities, claims
and dependencies; local JSON receipts additionally retain automatic-label
ownership, ready age, retry deadlines and revision checks. Back up this state.
Losing it safely pauses untriaged work and conservatively treats existing
priorities as manual; it does not grant ownership of stale claims.

The public service defaults to `FORGE_TRIAGE_MODE=local`. Local triage uses p1 for security/bug labels and p2 for
other new priorities, with the same dependency validator. Model/auth/validation
failure blocks untriaged work and sets a 15-minute retry cooldown; it does not
silently downgrade to local judgment. Input batches over 40 ready issues and
closure over 150 dependencies fail closed. The oneshot service has a 10-minute
time budget; per-repository model calls have a 120-second deadline.

This is deterministic selection with a legacy cooperative builder. The existing
workers still perform GitHub claims and transitions, so this release does not
make label claims atomic or revoke ambient credentials. Enforced assignments,
isolated production Codex/Claude execution, and a trusted sole merger remain
separate migration gates in implementation.md.
