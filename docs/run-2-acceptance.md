# Run 2 acceptance

Run 2 groups the worker boundary in #255, #259 and #265 with the first CLI
setup improvements in #271. This record distinguishes implementation checks
from live qualification. None of the broad parent issues is closed by this run.

## Implemented boundary

- The host freezes source commits, notes, declared Git dependencies, policy and
  publication authority before launch. Resumed and recovery jobs retain their
  exact previous inputs. Reusable policy identity is separate from each job's
  immutable source snapshot.
- Every worker uses a read-only Git mirror with per-job receipts. Declared
  dependencies and installed organization repositories are allowed subject to
  actual App access; a project deny list overrides both. Refused fetches produce
  claims. Request, byte and receipt limits bound the host resources used by a job.
- Worker GitHub tokens and direct GitHub publication have been removed. The host
  verifies retained bundles, creates the exact output tree through the selected
  qualified App strategy, reconciles uncertain effects and checks the final PR.
  Review and Audit receive facts bound to that exact confirmed candidate.
- Each job has its own internal network and bounded work volume. Trusted Git
  and provider/registry endpoints are separate containers. Native Linux checks
  qualify isolation, live revocation, read-only Git, quotas and stopped capture.
- A terminal host launch refusal is recorded before verified cleanup and becomes
  an uncharged owned wait. Mirror access refusal requires a decision; ordinary
  issue prerequisite checks cannot clear it. Transient preparation failures have
  a durable retry time and still honor cancellation and the execution deadline.

## Validation record

Independent review found and prompted fixes for policy/source collisions,
missing resumed-source pins, unbounded denied-fetch receipts, missing host facts
in Review/Audit notes and incorrect automatic clearing of host access refusals.
Focused regressions cover those findings and exact publication/replay guards.

Real npm and pip VCS installs passed through the local read-only Git endpoint
with external GitHub HTTP refused and no GitHub credential. The pip subprocess
used Python 3.12 because the local Python 3.14 installation's pyexpat module could
not load; this is an environment limitation, not a skipped compatibility check.

Combined UI unit tests (585), typecheck, production build and browser suite
passed. The initial backend batch exposed old contract fixtures and the source
file-mode/canary-receipt bugs described above. All 142 tests in the combined
fixes batch passed. After integrating main's cleanup changes, all three backend
shards, UI unit/type/build/browser checks and source audit passed in
[CI on `0ee4941`](https://github.com/XRPL-Commons/forge/actions/runs/38549115143).

## Live qualification

- The approved scratch signing experiment completed on 2026-10-14. Controller
  App `4959034` created REST commit `a914efb78a0e5e12f66dbbfe6261cd3a86a7359f`
  without custom author/committer fields; GitHub reported a valid verified
  signature and the exact ref update succeeded. Rule suite `4518978971` passed
  organization signing rule `20606309`. Unsigned control commit
  `ad57f6617be3fc2df67428721d471ec72fcb743f` was refused with the ref unchanged;
  suite `4518979811` explicitly failed the same required-signatures rule. The
  active rule had no bypass actors. REST therefore works for this installation;
  the conditional GraphQL fallback was not needed.
- The workflow-file attempt was refused with HTTP 403 and an unchanged ref.
  The installation lacks Workflows write, so positive workflow support remains
  unproven. The temporary token was revoked. Scratch repository `1418653585`
  was deleted and an independent authenticated read returned 404.
- This experiment does not synthesize a production qualification record. The
  supported qualification CLI must still record its own operation bound to the
  target repository, branch and current rules before enabling host publication.
- The Controller's current installation lacks Workflows write permission. Test
  that limitation and report it; do not expand permission as part of the probe.
- Anvil's native boundary canary passed on Ubuntu 24.04 and Docker 29.1.3,
  revision `02cd206`, image
  `sha256:a5d60fa4463e13574ceadbbfc3ccfe3d03ec3ca3fc9a29b48b5f6c3be3b5c8c6`.
  Real Git clone/fetch, denied push, provider TLS, npm/pip downloads, GitHub/host/
  cross-job denial, external DNS denial, immutable inputs, disk quota, session
  revocation and stopped capture passed. Disposable cleanup was confirmed.
- Anvil's supported upgrade completed successfully at `936859b`, schema 040.
  All four services are active; automation and merging remain disabled. Verified
  cleanup removed the formerly stuck probes. Fixed termination/reuse probes now
  send an empty credentials envelope because their fixed programs make no
  provider calls; provider probes retain normal authentication. Forty-one
  focused tests, independent review and
  [full CI](https://github.com/XRPL-Commons/forge/actions/runs/38557722035) passed.
- The seven-role canary stopped before worker launch because fresh setup had no
  configured repository policy row. Live read-only checks confirmed all four
  Anvil Apps and successful access to the selected private repository. The
  correction gives only the disposable canary an explicit policy for that
  selection; configured deny rules and actual App access remain authoritative.
  Its fresh-store regression and all seven source/broker paths passed focused
  tests and independent review. Live role qualification remains pending.
- The native boundary canary also passed on `e430a9c` and its rebuilt image
  `sha256:f75e743bd807d728e9e810d7fca798cd0da5728dadebe737806dce42f4adc172`.
  All nine checks passed and disposable cleanup was confirmed.
- Complete the signed-publication and every-role canaries before claiming the
  boundary accepted or deploying it to the active Forge factory. The boundary
  canary uses direct fixture workers, so it does not establish production role
  harness compatibility. The supported `setup qualify-roles` and `setup qualify-publication` commands
  now provide those checks. Role candidates are explicitly disposable fixtures;
  signing qualification requires a host-recorded API operation and exact
  independent read-back. No fixture result certifies a real PR or merge.

Forge remains on Run 1 during its separate 24-hour observation. The latest
ownership sweep found 13 current memberships, all owned, with no orphans. See
[Run 1 acceptance](run-1-acceptance.md) for the release and observation window.

## Explicit limits

Existing draft PRs require readiness before host publication; the refusal is
`publication_draft_requires_ready`. The publisher does not assume the Controller
may promote another App's draft. Unsupported Git tree modes under a GraphQL
strategy remain explicit claims. The new steward, one-App identity migration,
provider credential isolation and final fresh-install smoke merge belong to
later runs in [the implementation plan](run-plan.md).

## Concurrent main changes

Cleanup PRs #274, #276 and #277 merged while this branch was being validated.
Their changes are retained: removed legacy import tooling stays removed, `uv`
installs the CLI, and API mutations require a signed-in operator. The supported
`forge migrate upgrade` path remains. Run 2's mirror service, narrow rule repair
and qualification commands are integrated with that updated base. Combined CI
passed, including the subsequent source-transfer correction on `02cd206`
([run](https://github.com/XRPL-Commons/forge/actions/runs/38550713982)). The new
qualification commands passed independent review and 63 combined focused tests.
CI caught personal paths in documentation/test examples; generic paths now pass
the source audit and all 17 affected tests. The cleanup repair passed full CI on `62e4999`
([run](https://github.com/XRPL-Commons/forge/actions/runs/38553600750)), before the
standalone packaging failure was found on Anvil.

Main's SQLite-only runtime deletion (#279) is now incorporated. Tokenless
Discussion startup and setup-specific ProbeRunner cleanup use the surviving
modules. Follow-up #280 joins this gate: fresh stores can pair agents using their
existing UUID identity; discussion leases and memory retries do not block
unrelated accounts or paused repositories; a deployment candidate failure leaves
other candidates and discussions progressing and clears on a successful retry.
Codex login admission now checks the stored account locally before dispatch,
with an account-scoped auth block that preserves independent provider limits.
Deployment regressions passed 69 tests, fresh pairing passed 28 tests, login
admission passed 43 tests, and the standalone migration bundle passed 53 tests
before this integration. Independent review covered the merge and these changes.
The full combined CI gate passed on `e430a9c`
([run](https://github.com/XRPL-Commons/forge/actions/runs/38555781186)). The final
probe correction passed its full CI gate and completed Anvil's upgrade.
