# Implementation status

Forge is an experimental coordination system. The tmux supervisor is the working compatibility path: it manages configured role sessions, checks queue ownership and capacity, records local activity, and releases idle workers after verified handoffs. The dashboard uses local queue snapshots, activity history, and collected GitHub evidence.

Structured adapters, atomic attempt state, policy validation, provider circuit breakers, isolated executor fixtures, and independent-observer modules are included as staged components. Their presence and passing synthetic tests do not mean production repository execution through those paths is enabled or that a complete security boundary has been established.

## Remaining integration gates

The GitHub settings layer is implemented: file-backed configuration, App-authenticated
read-only installation sync, role access checks, explicit repository activation,
and admission guards. Per-role credentials are not yet injected into live legacy
harnesses. Settings readiness means access has been verified; it is not evidence
of execution isolation, branch-protection configuration, or completed migration.

- Isolate worker credentials, repository tools, and host resources with dedicated identities and tested containment.
- Connect structured attempts to live reconciliation and safe restart/cancellation behavior.
- Separate trusted merge credentials and validate current candidates against effective repository policy.
- Provision an independent observer mirror with an explicitly reviewed baseline and complete accepted-patch coverage.
- Prove bounded provider handoff, state-loss recovery, and cleanup before production expansion.

## Practical limits

Same-user tmux workers retain their ambient authority. Role prompts and a cooperative build wrapper are operating conventions, not credential isolation. Terminal activity is observational and does not prove task assignment, start time, or completion. Holds and admission gates cannot revoke an already running worker's capabilities. GitHub evidence and approvals remain authoritative.

The public role templates require repository protections and do not inherit administrative merge authorization from any existing fleet. Operator-managed deployment, credentials, and policy review are required.

See [capacity](capacity.md), [scheduling](scheduling.md), [worker recovery](worker-recovery.md), and [executor policy](executor-policy.md).
