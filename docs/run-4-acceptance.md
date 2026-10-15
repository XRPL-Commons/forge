# Run 4 acceptance

This run combines the remaining Steward and delivery changes in #266/#267
with the single-App and operator identity work in #259. The operator confirmed
that the default repository setting requires independent Forge Review and
Audit for human-authored PRs as well as Forge-created PRs.

## Delivered behavior

- Triage and conversations share stored Steward decisions. Build result v2
  carries typed claims tied to the actual assignment and member revisions.
- Schema 4 consolidates delivery phases and operator writes while retaining
  history, uncertain sends, bounded retries and independent holds. The four
  gate classes retain historical reason codes.
- A human PR has a review-only unit with immutable source information and
  independent Review/Audit jobs. It has no invented issue, Builder, branch
  publication or automatic merge. Changes to its source invalidate old evidence.
- Fork input comes from the base repository's authorized PR ref through the
  read-only mirror. Workers receive no GitHub credentials.
- Repository scope defaults to all PRs and can be changed in the repository UI.
  Changes use revision checks and invalidate outdated protection evidence.
- The supported identity CLI preserves retired App identities, pending work,
  operators and agent grants. Trusted Tailscale identity and same-App GitHub
  sign-in are supported. Sign-out revokes connected agents.

## Combined validation

The local bulk backend run exercised 2,248 tests and found three stale fixtures:
a missing supervisor component, an incomplete mocked settings document, and
protection evidence missing the new scope. All 47 tests in the corrected modules
passed. The combined revision passed all backend shards, UI and source audit in
[CI](https://github.com/XRPL-Commons/forge/actions/runs/38713523760), then merged
as `e12960c` in [PR #297](https://github.com/XRPL-Commons/forge/pull/297).

All 591 UI unit tests, typecheck, production build and browser tests passed.
Focused tests cover schema upgrade, history retention, uncertain writes,
current-scope admission, fork source, independent results, sign-out and restart.

Independent review found two shared-commit problems: retiring a peer could
leave another PR's check pending, and a Forge merge could publish success
before a human peer passed its reviews. Both corrections passed independent
review and all 16 authority regressions, including the composed Forge/human
case. GitHub checks belong to a commit, so every success publisher validates
the current peers sharing that commit.

## Deployment and live acceptance

Both existing installations completed the supported CLI upgrade from
`c0f2a01`, schema 3, to `e12960c`, schema 4. The CLI reported `upgraded`, stage
`complete`, and no pending migrations. Anvil's automation remains disabled.

Anvil's real-provider canary qualified Build, Review, Audit and Recovery on
image `sha256:da56f94867613b5a7b7eb20a59834c5e74790439707dbcae5e9398ef5e00e594`.
Triage then failed before provider execution because its new shared assignment
omitted the publisher required by the worker harness. Conversation assignments
had the same omission. Real assignment fixtures reproduce both failures through
the harness entry point. The shared assignment builder is corrected; the
remaining live roles must pass after deployment. Disposable cleanup was
confirmed and automation stayed disabled throughout the failed canary.

Identity migration is a separate supported CLI operation. New App permissions,
callback configuration, installation changes and repository rules require their
actual configuration and verification before declaring the single-App rollout
complete. Existing stronger repository rules are preserved.

The following remain live acceptance work: production role canaries, resumable
App/identity migration, actual human PR Review/Audit, and the fresh-install
issue-to-merge measurement. Run 5 also retains installation/consent cleanup and
provider credential isolation. Broad parent issues remain open until their
complete acceptance is demonstrated.
