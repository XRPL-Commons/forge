# Run 1 acceptance

## Release and checks

- [PR #272](https://github.com/XRPL-Commons/forge/pull/272) merged as
  `098e38fd69114a5e744896a6578b8f1820c4359b` on 14 October 2026.
- [CI run 38538369509](https://github.com/XRPL-Commons/forge/actions/runs/38538369509)
  passed source audit, all three backend shards, the required aggregate backend
  check, and UI unit/type/build/browser checks. The independent combined review
  and review of its subsequent fixes found no remaining blockers.
- The supported resumable CLI upgrade applied schema 033 through 038. Its final
  receipt reports `upgraded`, stage `complete`, schema 038, no pending migrations
  and no blockers, at the exact merged release.
- Native host reconciliation completed at **2026-10-14 09:10:47 UTC**. The status,
  supervisor and UI services are active. Codex 0.153.4 default-model qualification
  passed; optional Claude qualification is still required and Grok is unconnected.

## Deployed artifacts

| Component | Identity |
| --- | --- |
| Standard runner | `sha256:3864cf13ce98ceb6f84d73b2f8add3657210dc9af9227e980b3edae76604cb40` |
| Full runner | `sha256:937c209893c72379d288940be956bd7910e9b4393f5a8a778525da70081c8ffc` |
| UI | `670313454fa3f1c2` |
| Schema | 038 |

These identities were read from the active SQLite configuration, not the old
diagnostic JSON files. Successful CLI reconciliation includes the native runner
build-environment canaries before switching the configured images.

A separate offline canary on the deployed standard image also passed: the
native Codex tool sandbox could not read, overwrite or rename either private
exit receipt, and the parent confirmed both synthetic receipts were unchanged.
The canary had no network or real credentials and its container was removed.

## Live observation

Observation starts at the completed host reconciliation above. The first
read-only SQLite ownership sweep at **2026-10-14 09:22:14 UTC** found 13 current
work members, all with a live/captured job, current next action, pending effect
or applicable gate; zero scheduling orphans. The factory is enabled, observation
reported no errors, and a build resumed for the approved Academy group.

**The 24-hour acceptance window is not complete.** An initial healthy sweep is
not evidence that every incident scenario has succeeded in production. Continue
checking ownership, bounded retries, cleanup and user-visible exits. Any release
change or discovered failure must be recorded with its effect on acceptance.

## Subsequent releases

The initial release did not remain unchanged for 24 hours. Production later
advanced through the runtime cleanup and Run 2. The supported upgrade completed
on `d6113d390a22d1223f0854f25cb5f1813d41235d`, schema 040, at
**2026-10-14 12:09:29 UTC**. The release receipt matches the source, all four
managed services are active, and the updater exited. This is the new completion
baseline; the original Run 1 window cannot be claimed complete.

Before that upgrade, source had advanced while the database remained at schema
038. The mismatch was observed explicitly, admission was drained through the
supported upgrade path, and the subsequent deployment completed reconciliation.
The last ownership sweep before completion found 13 owned members and no
orphans. That sweep does not replace observation of the new release.

## Failure encountered during deployment

The first apply failed before writing a new upgrade journal. Source and schema
remained unchanged. A real schema-033 regression reproduced the same failure
when another writer held SQLite's write lock; releasing the lock allowed the
same resumable operation to complete. The second live apply succeeded.

Run 2 includes a focused diagnostic fix that reports SQLite's safe error code
and retryable busy classification. It changes neither admission, lock timeout
nor retry policy. The original live error did not retain its SQLite code, so
lock contention is the supported explanation, not a directly captured code.

## Remaining scope

Generic host-runner and unreadable-store faults remain open under #262. Worker
GitHub credentials are removed with Run 2's host publisher and read-only mirror;
Run 1 does not claim that boundary. Broad parent issues stay open until their
complete acceptance criteria pass.
