# Open-source readiness review

Reviewed on 2026-09-13 against the durable factory implementation on `feat/github-app-settings`.
This review covers source distribution; it does not certify production isolation
or authorize publishing the repository.

## Source and confidentiality

- Gitleaks 8.30.1, using its default rules and redacted reports, found no secrets
  in the working source or Git history. All reachable refs were included; the
  scanner binary was verified against the release asset's SHA-256 before execution.
- Additional inspection found no private key blocks, common credential-token
  formats, credential-bearing database/HTTP URLs, or developer home paths in the
  current source. Deployment-specific account/project identifiers in test fixtures
  were replaced with synthetic names. Public project URLs, GitHub SSH syntax, and
  normal Git author attribution are not credentials and remain where appropriate.
- Private configuration, credentials, logs, database files, dependency folders,
  backups, and execution state are excluded from version control. The new
  `tools/check_public_source.py` checks tracked paths for these artifact classes.
- CI now checks tracked artifacts and runs a checksum-pinned Gitleaks binary
  against both source and history. Findings are redacted; no blanket exception
  excludes tests, documentation, or configuration examples.

Automated scans are evidence, not a guarantee that every confidential detail has
been recognized. GitHub-hosted issues, PR discussions, release assets, workflow
logs, remote un-fetched refs, and private runtime directories were not part of this
source review. Review those separately before changing repository visibility.
This review did not rewrite Git history, rotate credentials, or publish anything.

## Defaults and documentation

- `examples/factory.json.example` validates and starts with no connections or
  enabled repositories. Sync is every five minutes; observations expire after
  fifteen minutes. Both values are validated and editable.
- `examples/github-credentials.env.example` contains placeholder key-file paths,
  not keys. Human App creation, selected-repository installation, key storage,
  role grants, activation, and migration are documented in `github-settings.md`.
- A local dashboard preview can bind to loopback without Tailscale. Production
  usage still requires a trusted private network; there is no per-user login.
- Both factory and runtime examples disable automation and contain no account or
  repository inventory. New capacity defaults are four Build, two Review, one Audit
  and one Recovery slot under a shared cap of eight. Legacy examples stay separate.
- The runner image build context excludes everything except its source and policy.
  Codex is version-pinned; production image digests, App keys and provider login
  are supplied by the operator. Native sandbox/egress configuration and exact
  protection requirements are documented in `factory-runtime.md`.
- Documentation distinguishes implemented behavior, tested boundaries, legacy
  components, and the authenticated qualification still required before cutover.

## Verification

The final Linux backend suite passed 859 tests (four optional tests skipped) in an
isolated source copy verified against the committed source. The observer suite
passed 68 tests. The local factory, adapter, settings and HTTP checks also passed
77 tests. All three dashboard, factory and settings browser suites passed locally; desktop/mobile
screenshots were inspected. Coverage includes complete
sequential routing across supervisor restarts, rejection and rebuild, exact PR
publication recovery, stale revisions, mandatory investigations, shared retry
budgets, missing credentials, lost resources and same-origin operator controls.

Real credential-free Linux canaries demonstrated full container teardown, repeat
reset, slot reuse, stale-reset protection and the pinned image's native sandbox:
repository writes succeeded while a synthetic provider file and outside writes
were denied. The sandbox test used a unique temporary AppArmor profile and removed
it afterward; it did not relax global host settings. No test used real GitHub or
model credentials or modified the running fleet. These checks do not establish
that an authenticated coding workflow is production-ready.

The full lifecycle suite targets Linux. The Mac environment has existing Linux
`/proc` and path-identity incompatibilities. Optional real-systemd canaries were
not enabled. The complete Linux suite includes the final issue-reference,
hold/cancellation, running-output and GitHub completion reconciliation fixes.

## Deployment and publication boundaries

The new supervisor, role-scoped App credentials, resettable runner, recovery flow,
settings and dashboard are implemented. Live cutover still requires operator-owned
App installations/keys, provider authentication, native runner host policy, internal
network/proxy, required branch protections and a credentialed pilot repository.
The source does not install those changes or activate the existing fleet.

Source publication and production cutover are separate decisions. See the
[workflow contract](software-factory-spec.md), [implementation status](implementation.md)
and [runtime setup](factory-runtime.md). This review does not authorize a push or a
repository visibility change.
