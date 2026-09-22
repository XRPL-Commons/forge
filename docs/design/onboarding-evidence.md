# Operator onboarding verification

Production pages, rendered with synthetic GitHub/service responses in Chrome on
2026-09-16. These screenshots document interface checks, not a live deployment
or a completed GitHub pilot. Current main's Settings navigation and mobile
layout at `101d66e` are integrated. These pages are now served by the operator
UI (`forge-ui`, see [`docs/ui-deployment.md`](../ui-deployment.md)); its
equivalent checks are `cd ui && npm run test:e2e`, with `FORGE_BROWSER_PATH`
pointing to a Chrome executable. Tests use temporary browser profiles with
Chromium sandboxing enabled. The repository-flow test serves the
real generated ruleset JSON from a temporary loopback HTTP server and checks the
downloaded bytes and filename.

## Screens reviewed

- [Prepare repository from onboarding, 1280×800](onboarding-evidence/forge-onboarding-prepare.png)
- [Download and import, desktop](onboarding-evidence/forge-ruleset-desktop.png)
- [Existing ruleset, 390×844 mobile](onboarding-evidence/forge-ruleset-mobile.png)
- [Verified baseline and bypass explanation](onboarding-evidence/forge-ruleset-ready.png)
- [Missing service heartbeat](onboarding-evidence/forge-onboarding-service-wait.png)
- [Factory status and a synthetic merged issue](onboarding-evidence/forge-factory-desktop.png)

Checks cover primary actions within the laptop viewport, mobile overflow,
640-pixel reflow for a 200% desktop viewport, keyboard focus during unchanged
polling, reduced motion, conflicts, failed/stale observations, reloading, account
progress, explicit Start, reset recovery and actionable factory states. The six
browser suites pass. The new download never grants access or installs rules.

## Functional checks

- The 98-test focused settings/onboarding/repository/factory-cycle/feedback set passes.
- All 51 factory tests pass.
- Full Python discovery: 988 tests, 50 failures, 46 errors, 4 skipped on this
  macOS sandbox. The untouched main checkout at `980e901` has exactly the same
  96 failing/error test names (953 tests total). The existing failures include
  Linux `/proc` assumptions, Bash-version expectations and execution isolation;
  this is not a clean Linux qualification run.
- Read-only API fixtures cover classic protection, effective inherited rulesets,
  combined requirements, malformed/unknown responses, pagination, CODEOWNERS,
  mixed repository readiness, branch changes, stale/config-bound downloads,
  selective prerequisite recovery and expiring paid-probe evidence.

The generated JSON and reader field names were checked against
[GitHub's rules API schema](https://docs.github.com/en/rest/repos/rules#create-a-repository-ruleset)
and [the JSON import guide](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/creating-rulesets-for-a-repository#importing-prebuilt-rulesets).
Rulesets use `require_code_owner_review`; classic protection uses
`require_code_owner_reviews`. App manifests and permission grants are unchanged.

## Still to qualify

Deploy the reviewed changes, choose a pilot repository, have its operator import
or edit the rules using their own GitHub session, and explicitly start Forge.
Follow a bounded issue through review, audit and a GitHub-confirmed merge; also
exercise the human-approval hold. Record those live results separately under #12.
