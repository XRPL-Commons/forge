# Open-source readiness review

Reviewed on 2026-09-11 against the settings implementation based on `6dfc22b`.
This review covers source distribution; it does not certify production isolation
or authorize publishing the repository.

## Source and confidentiality

- Gitleaks 8.30.1, using its default rules and redacted reports, found no secrets
  in the working source or Git history. All reachable refs were included; the
  history scan reported 21 scanned commits (the base has 22 reachable commits,
  including a merge commit). The scanner binary was verified against the release
  asset's SHA-256 before execution.
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
- Legacy examples remain disabled. Legacy capacity defaults remain conservative;
  the proposed four/two/one role allocation belongs to the new runner design and
  has not silently changed the existing supervisor's limits.
- Relative Markdown documentation links were checked. The design spec is included
  separately from implementation status so proposed behavior is not presented as
  an already deployed guarantee.

## Verification

The Linux backend suite passed 809 tests (four optional tests skipped), and the
observer suite passed 68 tests. After the final admission/cleanup changes, 132
focused Linux tests passed; the final settings/API checks passed 31 tests locally.
Existing fleet and new settings desktop/mobile browser checks passed. Coverage
includes permissions missing, access lost,
expired observations, installation suspension, renamed repositories, partial API
inventories, concurrent configuration edits, interrupted syncs, and activation
confirmation. HTTP tests cover Host/Origin/token enforcement and asynchronous sync.
Stop/Release requests are processed before disabled/access-failed admission gates;
direct triage and assignment reservation also enforce current access.

The full lifecycle suite targets Linux. The Mac run encountered existing Linux
`/proc` and path-identity assumptions; the complete suite was therefore run on
Linux in an isolated source copy with synthetic tools. Optional real-systemd
canaries were not enabled. No test modified GitHub or the running fleet.

## Remaining production work

The current change supplies GitHub setup, access discovery, explicit activation,
and admission guards. It does not yet replace shared legacy harness credentials
with per-role App credentials, implement the new resettable runner, or complete
the proposed supervisor/recovery state machine. GitHub branch protections and
authorized human approvers must still be configured and verified before cutover.

Source publication and production cutover are separate decisions. Use
`docs/software-factory-spec.md` for the agreed target and `docs/implementation.md`
for current execution limits.
