# Onboarding release audit

Audited 6 November 2026 from a new operator's perspective. This is a source and
local UI audit against `4a26cc0` plus this change, not a new live installation.
The north-star metric is time from starting installation to a verified first
merge. Operator actions and time spent waiting for a prompt are separate measures.

## The journey

| Operator question | Finding | Treatment |
| --- | --- | --- |
| What am I installing? | The README led with internals and offered competing CLI/manual entry points. | One quickstart, prerequisites, costs, browser handoff, and links for advanced operation. |
| What do I need to answer? | The CLI asks for owner, operators, repository names, per-repo policies, providers, login types, hostname, smoke target and fingerprint. Many are defaults a beginner cannot evaluate. | Group A below. |
| Why am I back in a terminal? | Codex can connect from installation progress, but routine account recovery still presents commands to run as the server's service account. | Group B below; do not describe this as completed web onboarding. |
| How many GitHub Apps do I need? | Fresh web setup still requested four roles despite the single-App backend. | Fresh setup requests one App. Existing partial legacy setup retains its continuation path. |
| Is anything happening? | The installation page has real stages, timestamps, blockers and actions, but repeats reassurance and describes API internals. | Shorter copy; retain elapsed time, last progress, observation freshness, errors and actions. |
| Can I enable my repository? | Successful access and policy checks each required another Continue click. | Choose repository → confirm settings and enable. Failed checks stay on the same screen with a retry. Installation completion still gates activation. |
| Where are settings? | Five categories compete for attention. Capacity and reset appear alongside normal operation. | General, Repositories, Connections. Fold capacity/reset; preserve deep links and unsaved edits. |
| What should I do first? | Installation tests itself, but the README buried the path to real issue work. | Explain `ready`, planning groups, approval and Activity. Validate this handover in Group C. |

## Delivery groups, in order

### A — Finish the default installation path (release blocker)

Track in [#271](https://github.com/XRPL-Commons/forge/issues/271).

- Reduce the default interview to host, factory name, owner, repositories and
  coding account. Show one editable summary before applying. Keep hostname,
  extra operators, smoke target and other customization under an advanced choice.
- Offer repository/provider choices rather than requiring their internal spelling.
  Keep the deterministic JSON intent surface for agents.
- Retain explicit workflow-write permission and host identity verification.
  Fewer questions must not silently grant broader access.
- Complete the early browser-consent sequence. Current code still needs host
  preparation before some approvals; “all consent before apply” is a target,
  not an achieved property. Show the handoff point at which closing the terminal
  is safe, and one resume action when it is not.
- Keep setup progress visible throughout; a paused step must name the action or
  dependency it is waiting for. Preserve passkey/missing-manifest recovery and
  expiring device-code recovery.
- Reconcile the older #271 requirements: no automatic ruleset creation/repair,
  no paid-plan prerequisite. Observe owners' existing GitHub protections.

**One PR:** interview, consent/handoff behavior, CLI help and canonical setup
instructions. Test old saved intents and interrupted sessions with that change.

### B — Finish browser recovery and routine settings (release blocker)

Track in #271, with the identity/account contracts from
[#259](https://github.com/XRPL-Commons/forge/issues/259).

- Extend the existing account-operation API beyond installation so an operator
  can reconnect or verify a coding account from Connections. Reuse its stored
  operation, expiry and progress state; do not add a second login implementation.
- Make provider limitations explicit. Codex browser login, other browser providers
  and API-key/token modes need separate capability checks. Do not offer buttons
  that only reveal SSH commands after clicking.
- Use a single connection entry point, one current status, and one next action.
  Keep scope/credential consent visible when approval is actually needed.
- Audit the linked setup/help reference pages for four-App language, obsolete
  ruleset steps and terminal-only instructions. Keep historical migration detail
  in reference documentation, outside the default journey.

**One PR:** account API lifecycle, UI recovery and corresponding help. Validate
expiry, concurrent requests, interrupted login, revocation and reconnect together.

### C — Prove the release journey (acceptance batch)

Track in #271; record the batch in
[#216](https://github.com/XRPL-Commons/forge/issues/216).
Run after A and B rather than reinstalling after each small fix.

1. A new operator follows only the README on a clean supported VM. No assistant
   edits server files, copies secrets, patches state or runs undocumented commands.
2. Connect two repositories, including one with existing protections. Observe
   those protections; do not add a Forge ruleset or alter subscription/visibility.
3. Exercise a passkey interruption, expired provider code, lost CLI connection,
   reload of web progress and resume. Verify there are no duplicate Apps, logins,
   scratch repositories or smoke issues.
4. Verify the smoke issue's build, independent review/audit, CI and actual merge.
   Confirm selected repositories stay disabled until handover.
5. Run a real issue with human approval required. Show the approval hold in the
   UI, confirm the approved revision merges, and follow the result from Activity.
6. Record total time, attended time, automatic time, browser/terminal actions,
   retries and stage timings. Report cold installation separately from cached
   installation. Existing repaired runs do not establish a clean benchmark.

Retain the ≤12-touch / no-attended-wait target from #271, but measure it before
claiming it. There is no defensible minimum wall-clock estimate yet: artifact
transfer, provider latency and the actual build/review/audit path dominate it.

## Keep out of the release's critical path

- Mac hosting and Xcode execution: [#327](https://github.com/XRPL-Commons/forge/issues/327), deferred.
- New coding adapters: [#98](https://github.com/XRPL-Commons/forge/issues/98),
  [#5](https://github.com/XRPL-Commons/forge/issues/5), deferred.
- Architecture tracks #269/#267/#258, publication #255, sandbox #265 and planning
  #215 remain separate. Pull a concrete unresolved item into the release only
  when it blocks the supported install, safe execution or a verified merge.
  This UI audit is not evidence to close those issues.

## Validation for this change

Group UI checks around complete journeys: fresh and partial legacy GitHub setup;
repository selection, failed/stale access, installation hold and explicit enable;
settings save/discard and deep links; connection navigation; installation progress
and device-code handling. Run unit tests, typecheck, production build and these
browser journeys once against the integrated change, then repeat only failures
or checks affected by subsequent edits. Live clean-install acceptance is Group C.
