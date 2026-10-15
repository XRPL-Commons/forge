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
passed. The final combined revision must also pass full CI.

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

Read-only CLI preflight on both existing installations reported schema 3,
pending migration 4 and no blockers, at source `c0f2a01`. Deployment uses
`forge migrate upgrade` with the exact merged release and a resumable apply.
This preflight is not a deployment or live acceptance result.

Identity migration is a separate supported CLI operation. New App permissions,
callback configuration, installation changes and repository rules require their
actual configuration and verification before declaring the single-App rollout
complete. Existing stronger repository rules are preserved.

The following remain live acceptance work: production role canaries, resumable
App/identity migration, actual human PR Review/Audit, and the fresh-install
issue-to-merge measurement. Run 5 also retains installation/consent cleanup and
provider credential isolation. Broad parent issues remain open until their
complete acceptance is demonstrated.
