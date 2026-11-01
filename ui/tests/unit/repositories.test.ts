import { describe, it, expect } from 'vitest'
import {
  ACCESS_LABELS, ACCESS_STATUS_NAMES, PROTECTION_LABELS, ROLE_LABELS_APP, ROLE_PURPOSES,
  actionMessage, checkedAtText, connectionAction, connectionCards, hintText,
  lastRefreshText, repoDescription, repoStage, repositoryRows, rowState,
  statusPillText, deploymentCard,
  type DeploymentProfile, type DeploymentProposal, type DeploymentRow, type FactoryDeployment,
  type ProtectionReport, type RepositoryRow, type RoleAccess, type SettingsView,
} from '../../app/utils/repositories'

function row(overrides: Partial<RepositoryRow> = {}): RepositoryRow {
  return {
    id: 2, name: 'XRPL-Commons/hackathon', enabled: false, configured: true,
    access: { build: 'ready', review: 'ready', audit: 'ready', controller: 'ready' },
    access_ready: true, dispatch_allowed: false,
    protection_status: 'ready', protection_ready: true,
    protection: { repo_id: 2, branch: 'main', observed_at: 1790000000, available: true, requirements: [], sources: [], errors: [], additional_rules: [], code_owner_reviews: false },
    protection_previous_status: null, protection_last_known: null,
    rules_url: 'https://github.com/XRPL-Commons/hackathon/settings/rules',
    setup_url: '/settings/repositories/2?setup=1',
    ...overrides,
  }
}

const access = (overrides: Partial<RoleAccess> = {}): RoleAccess => ({
  status: 'ok', missing_permissions: [], required_permissions: {}, reason: null,
  observed_at: 1790000000, installation_url: null, app_slug: null, ...overrides,
})

describe('copy maps', () => {
  it('names the four Apps, their purposes and their badges', () => {
    expect(ROLE_LABELS_APP).toEqual({ build: 'Builder', review: 'Reviewer', audit: 'Auditor', controller: 'Controller' })
    expect(ROLE_PURPOSES.controller).toBe('Coordinates issues and merges approved pull requests.')
    expect(ACCESS_STATUS_NAMES.ready).toBe('Access verified')
    expect(ACCESS_STATUS_NAMES.missing_permissions).toBe('Permissions missing')
    expect(ACCESS_LABELS.missing_access).toBe('Needs repository access')
    expect(ACCESS_LABELS.stale).toBe('Refresh to check access')
    expect(PROTECTION_LABELS.needs_setup).toBe('Setup needed')
    expect(PROTECTION_LABELS.unchecked).toBe('Checking setup…')
  })
})

describe('repoStage', () => {
  it('is 0 with no repository', () => expect(repoStage(null, true)).toBe(0))
  it('is 1 while App access is incomplete', () =>
    expect(repoStage(row({ access_ready: false, protection_status: 'access_needed', protection_ready: false }), true)).toBe(1))
  it('is 2 while repository checks are not complete', () =>
    expect(repoStage(row({ protection_status: 'needs_setup', protection_ready: false }), true)).toBe(2))
  it('is 3 when the repository is ready but not enabled', () => expect(repoStage(row(), true)).toBe(3))
  it('is 4 when the repository is enabled on a live factory', () => expect(repoStage(row({ enabled: true }), true)).toBe(4))
  it('stays 3 during initial setup, where enabling is not available yet', () =>
    expect(repoStage(row({ enabled: true }), false)).toBe(3))
  it('keeps the previous status while a re-check runs', () =>
    expect(repoStage(row({ protection_status: 'checking', protection_ready: false, protection_previous_status: 'ready' }), true)).toBe(3))
})

describe('rowState', () => {
  it('reports an enabled, healthy repository', () =>
    expect(rowState(row({ enabled: true }))).toEqual({ icon: 'enabled', label: 'Enabled' }))
  it('counts protection as ready while a re-check runs over a ready repository', () =>
    expect(rowState(row({ enabled: true, protection_status: 'checking', protection_ready: false, protection_previous_status: 'ready' })))
      .toEqual({ icon: 'enabled', label: 'Enabled' }))
  it('flags an enabled repository whose protection is gone', () =>
    expect(rowState(row({ enabled: true, protection_status: 'needs_setup', protection_ready: false })))
      .toEqual({ icon: 'attention', label: 'Needs attention' }))
  it('flags an enabled repository that lost access', () =>
    expect(rowState(row({ enabled: true, access_ready: false }))).toEqual({ icon: 'attention', label: 'Needs attention' }))
  it('reports a repository that is not enabled', () =>
    expect(rowState(row())).toEqual({ icon: 'off', label: 'Not enabled' }))
})

describe('repoDescription', () => {
  it('invites the operator to enable a ready repository', () =>
    expect(repoDescription(row(), true)).toBe('Setup is complete. Enable automation to let Forge pick up eligible issues in this repository.'))
  it('says nothing is left to do once automation is on', () =>
    expect(repoDescription(row({ enabled: true }), true)).toBe('Setup is complete and automation is enabled. No action is needed here.'))
  it('sends the operator back to setup when the factory is not live', () =>
    expect(repoDescription(row(), false)).toBe('Repository setup is complete. Continue to finish setting up Forge.'))
  it('asks for repository verification', () =>
    expect(repoDescription(row({ protection_status: 'needs_setup', protection_ready: false }), true))
      .toBe('Check repository access and merge policy before enabling Forge.'))
  it('asks for the missing Apps', () =>
    expect(repoDescription(row({ access_ready: false, protection_status: 'access_needed', protection_ready: false }), true))
      .toBe('Connect the Forge App before continuing.'))
  it('explains the check while it is unknown', () =>
    expect(repoDescription(row({ protection_status: 'unknown', protection_ready: false }), true))
      .toBe('Forge checks repository access and observes existing GitHub rules before enabling automation.'))
})

describe('statusPillText', () => {
  it('reads Enabled for a live, enabled repository', () => expect(statusPillText(row({ enabled: true }), true)).toBe('Enabled'))
  it('reads Ready to enable for a live repository that is off', () => expect(statusPillText(row(), true)).toBe('Ready to enable'))
  it('reads Ready to continue during initial setup', () => expect(statusPillText(row(), false)).toBe('Ready to continue'))
  it('appends the refresh suffix while re-checking a known repository', () =>
    expect(statusPillText(row({ protection_status: 'checking', protection_ready: false, protection_previous_status: 'ready' }), true))
      .toBe('Ready to enable · Refreshing…'))
  it('falls back to the protection label', () =>
    expect(statusPillText(row({ protection_status: 'needs_setup', protection_ready: false }), true)).toBe('Setup needed'))
})

describe('hintText', () => {
  it('points at the Apps when access is missing', () =>
    expect(hintText(row({ protection_status: 'access_needed', protection_ready: false }))).toBe('Finish connecting the Forge App above to continue.'))
  it('asks for a fresh check when the report is stale', () =>
    expect(hintText(row({ protection_status: 'stale', protection_ready: false })))
      .toBe('Check again to confirm the current GitHub settings before continuing.'))
  it('reports the GitHub error and the fallback report', () => {
    const report = { ...row().protection!, errors: [{ detail: 'Branch protection could not be read. Check Controller access and try again.' }] }
    expect(hintText(row({ protection_status: 'unknown', protection_ready: false, protection: report, protection_last_known: report })))
      .toBe('Branch protection could not be read. Check Controller access and try again. Showing the last checked requirements.')
  })
  it('reassures while a check runs', () =>
    expect(hintText(row({ protection_status: 'checking', protection_ready: false })))
      .toBe('This usually takes a few seconds. Automation stays unchanged.'))
  it('says nothing when the repository is ready', () => expect(hintText(row())).toBe(''))
})

describe('connectionAction', () => {
  it('links to the canonical installation page', () => {
    const url = 'https://github.com/organizations/XRPL-Commons/settings/installations/101'
    expect(connectionAction('build', access({ installation_url: url }), { registered: true, app_id: 1, install_url: null }))
      .toEqual({ label: 'Manage access ↗', href: url, external: true })
  })
  it('ignores an installation link that is not a GitHub installation page', () =>
    expect(connectionAction('build', access({ installation_url: 'https://evil.example/installations/1' }), { registered: true, app_id: 1, install_url: null }))
      .toBeNull())
  it('offers the App installation when Forge has no credentials yet', () => {
    const install = 'https://github.com/apps/forge-review-example/installations/new'
    expect(connectionAction('review', access({ status: 'not_configured' }), { registered: true, app_id: null, install_url: install }))
      .toEqual({ label: 'Install App ↗', href: install, external: true })
  })
  it('sends an unregistered role to setup', () =>
    expect(connectionAction('audit', access({ status: 'not_configured' }), { registered: false, app_id: null, install_url: null }))
      .toEqual({ label: 'Connect in setup →', href: '/onboarding', external: false }))
  it('offers nothing for a configured App with no canonical link', () =>
    expect(connectionAction('controller', access(), { registered: true, app_id: 4, install_url: null })).toBeNull())
})

describe('lastRefreshText', () => {
  const settings = (sync: SettingsView['sync']): SettingsView => ({
    ok: true, controls_token: 't', config: { version: 1, sync_interval_seconds: 300, access_max_age_seconds: 900, connections: {}, repositories: [] },
    revision: 'r', configured: true, config_file: 'factory.json',
    roles: { build: access(), review: access(), audit: access(), controller: access() },
    repositories: [], sync,
  })
  it('reports the last finished refresh', () => {
    const text = lastRefreshText(settings({ status: 'complete', finished_at: 1790000000 }))
    expect(text.startsWith('Last refresh ')).toBe(true)
    expect(text).not.toContain('Refresh interrupted')
  })
  it('appends the interrupted suffix', () =>
    expect(lastRefreshText(settings({ status: 'interrupted', started_at: 1790000000 })))
      .toBe('Last refresh ' + new Date(1790000000 * 1000).toISOString() + ' · Refresh interrupted; the previous data is shown.'))
  it('says so when access was never refreshed', () => {
    const never = settings({ status: 'never' })
    never.roles = { build: access({ observed_at: null }), review: access({ observed_at: null }), audit: access({ observed_at: null }), controller: access({ observed_at: null }) }
    expect(lastRefreshText(never)).toBe('Access has not been refreshed yet.')
  })
})

describe('connectionCards', () => {
  const settings: SettingsView = {
    ok: true, controls_token: 't',
    config: {
      version: 1, sync_interval_seconds: 300, access_max_age_seconds: 900,
      // `review` deliberately has no stored credentials: GitHub knows the App,
      // factory.json does not.
      connections: { build: { app_id: 1, private_key_env: 'K', installation_id: 101 }, audit: { app_id: 3, private_key_env: 'K', installation_id: 103 }, controller: { app_id: 4, private_key_env: 'K', installation_id: 104 } },
      repositories: [],
    },
    revision: 'r', configured: true, config_file: 'factory.json',
    roles: {
      build: access({ installation_url: 'https://github.com/organizations/XRPL-Commons/settings/installations/101', app_slug: 'forge-build' }),
      review: access({ status: 'not_configured', reason: 'Access sync failed; retry or inspect server configuration', observed_at: null }),
      audit: access({ status: 'missing_permissions', missing_permissions: ['contents: write'] }),
      controller: access({ status: 'ok' }),
    },
    repositories: [row({ id: 1, enabled: true }), row()],
    sync: { status: 'complete', finished_at: 1790000000 },
  }
  const onboarding = { phase: 'setup' as const, roles: {
    build: { status: 'ok', registered: true, app_id: 1, install_url: null },
    review: { status: 'not_configured', registered: true, app_id: null, install_url: 'https://github.com/apps/forge-review/installations/new' },
    audit: { status: 'missing_permissions', registered: true, app_id: 3, install_url: null },
    controller: { status: 'ok', registered: true, app_id: 4, install_url: null },
  } }

  it('describes each App once, in role order', () => {
    const cards = connectionCards(settings, onboarding)
    expect(cards.map(card => card.role)).toEqual(['build', 'review', 'audit', 'controller'])
    expect(cards[0]).toMatchObject({
      label: 'Builder', identity: 'forge-build', status: 'Connected', ready: true,
      observation: '2 repositories with verified access',
      action: { label: 'Manage access ↗', external: true },
      errors: [],
    })
  })
  it('marks a registered App without stored credentials as unverified and offers the install link', () => {
    const review = connectionCards(settings, onboarding)[1]
    expect(review.status).toBe('Installation unverified')
    expect(review.ready).toBe(false)
    expect(review.identity).toBe('forge-review')
    expect(review.observation).toBe('Access has not been verified.')
    expect(review.action).toEqual({ label: 'Install App ↗', href: 'https://github.com/apps/forge-review/installations/new', external: true })
    expect(review.errors).toEqual(['Access sync failed; retry or inspect server configuration'])
  })
  it('lists missing permissions as an error', () => {
    const audit = connectionCards(settings, onboarding)[2]
    expect(audit.status).toBe('Permissions missing')
    expect(audit.identity).toBe('App 3')
    expect(audit.errors).toEqual(['Missing contents: write'])
  })
  it('shows one shared App and retains every role permission failure', () => {
    const shared = { ...settings, config: { ...settings.config, connections: Object.fromEntries(['build', 'review', 'audit', 'controller'].map(role => [role, { app_id: 42, private_key_env: 'K' }])) },
      roles: { build: access(), review: access(), audit: access({ status: 'missing_permissions', missing_permissions: ['checks: write'] }), controller: access() } }
    const cards = connectionCards(shared, null)
    expect(cards).toHaveLength(1)
    expect(cards[0]).toMatchObject({ label: 'Forge App', identity: 'App 42', ready: false, status: 'Permissions missing', errors: ['Missing checks: write'] })
  })
  it('counts verified repositories only when all shared roles have access', () => {
    const shared = { ...settings, config: { ...settings.config, connections: Object.fromEntries(['build', 'review', 'audit', 'controller'].map(role => [role, { app_id: 42, private_key_env: 'K' }])) },
      roles: { build: access(), review: access(), audit: access(), controller: access() },
      repositories: [row(), row({ access: { build: 'ready', review: 'ready', audit: 'missing_access', controller: 'ready' } })] }
    expect(connectionCards(shared, null)[0]).toMatchObject({ label: 'Forge App', ready: true, observation: '1 repository with verified access' })
  })
  it('counts a single repository in the singular', () => {
    const one: SettingsView = { ...settings, repositories: [row({ id: 1, enabled: true })] }
    expect(connectionCards(one, onboarding)[0]!.observation).toBe('1 repository with verified access')
  })
})

describe('repositoryRows', () => {
  const rows = [row({ id: 1, name: 'XRPL-Commons/academy', enabled: true }), row({ id: 2, enabled: true, protection_status: 'needs_setup', protection_ready: false }), row({ id: 3, name: 'XRPL-Commons/docs' })]
  it('offers Disable for a healthy enabled repository and Resolve for a broken one', () => {
    const models = repositoryRows(rows, false)
    expect(models[0]).toMatchObject({ id: 1, name: 'XRPL-Commons/academy', to: '/settings/repositories/1', note: '', action: 'disable' })
    expect(models[1]).toMatchObject({ action: 'disable', note: 'Needs attention', resolve: '/settings/repositories/2?setup=1' })
    expect(models[2]).toMatchObject({ action: 'setup', to: '/settings/repositories/3?setup=1' })
  })
  it('sends every row to setup while Forge is still being set up', () => {
    expect(repositoryRows(rows, true).map(model => model.action)).toEqual(['continue', 'continue', 'continue'])
  })
})

describe('checkedAtText and actionMessage', () => {
  it('formats the last check as a wall-clock time', () => {
    const text = checkedAtText({ ...row().protection!, observed_at: 1790000000 })
    expect(text.startsWith('Last checked ')).toBe(true)
    expect(text).toContain(new Date(1790000000 * 1000).toISOString())
  })
  it('says nothing without an observation', () => expect(checkedAtText(null)).toBe(''))
  it('prefers the reason the API gave', () =>
    expect(actionMessage(new Error('Repository settings changed. Check again before enabling.'), 'Could not enable this repository.'))
      .toBe('Repository settings changed. Check again before enabling.'))
  it('falls back to the page sentence when the failure carries no reason', () =>
    expect(actionMessage(new Error('Forge did not accept the action.'), 'Could not refresh access. Try again.'))
      .toBe('Could not refresh access. Try again.'))
})

// ---------------------------------------------------------------------------
// Deployment card (spec "Operator UI": the Repository page's Deployment card).
// ---------------------------------------------------------------------------

const NOW = 1790000000

const proposal = (overrides: Partial<DeploymentProposal> = {}): DeploymentProposal => ({
  strategy: 'health',
  deploy: { workflow: '.github/workflows/deploy.yml', environment: 'production', trigger: 'push' },
  domain: { value: 'academy.xrpl-commons.org', source: 'workflow', confidence: 'high' },
  exposure: { kind: 'health', path: '/health', field: 'commit', header: null },
  commit_source: 'GITHUB_SHA',
  proposal: null,
  ...overrides,
})

const profile = (overrides: Partial<DeploymentProfile> = {}): DeploymentProfile => ({
  strategy: 'health',
  deploy: { workflow: '.github/workflows/deploy.yml', environment: 'production', trigger: 'push' },
  domain: 'academy.xrpl-commons.org',
  exposure: { kind: 'health', path: '/health', field: 'commit', header: null },
  workflows_digest: 'sha256:1', confirmed_at: NOW - 86400, exposure_issue: null,
  ...overrides,
})

const deployment = (overrides: Partial<DeploymentRow> = {}): DeploymentRow => ({
  status: 'idle', summary: null, proposal: null, profile: null, observation: null,
  error: null, reason: null, exposure_issue: null, reread_requested: false, ...overrides,
})

const observed = (overrides: Partial<FactoryDeployment> = {}): FactoryDeployment => ({
  name: 'XRPL-Commons/academy',
  live: { commit: 'abc1234def5678', at: NOW - 60 },
  deploy: { sha: 'abc1234def5678', state: 'success', at: new Date((NOW - 720) * 1000).toISOString(), run_url: 'https://github.com/x/y/actions/runs/1' },
  error: null,
  ...overrides,
})

describe('deploymentCard', () => {
  it('says nothing for a row the API did not describe', () => {
    expect(deploymentCard(null, null, NOW)).toBe(null)
    expect(deploymentCard(row(), null, NOW)).toBe(null)
  })

  it('shows what the strategist read, a prefilled domain and the three decisions', () => {
    const card = deploymentCard(row({
      deployment: deployment({
        status: 'proposed',
        summary: 'Deploys through GitHub Actions and exposes its commit on /health.',
        proposal: proposal(),
      }),
    }), null, NOW)!
    expect(card.state).toBe('proposed')
    expect(card.status).toBe('Proposal ready')
    expect(card.sentence).toBe('Deploys through GitHub Actions and exposes its commit on /health.')
    expect(card.lines).toEqual(['Strategy · Health endpoint', 'Deploy · .github/workflows/deploy.yml → production'])
    expect(card.exposure).toBe('Commit read from /health `commit`')
    expect(card.domain).toBe('academy.xrpl-commons.org')
    expect(card.showDomain).toBe(true)
    expect(card.actions).toEqual({ confirm: true, reject: true, reread: true })
    expect(card.observation).toBe('')
  })

  it('reads a header exposure and an issue that is only filed on confirmation', () => {
    const header = deploymentCard(row({
      deployment: deployment({ status: 'proposed', proposal: proposal({ exposure: { kind: 'header', path: '/', field: null, header: 'X-Build-Commit' } }) }),
    }), null, NOW)!
    expect(header.exposure).toBe('Commit read from header `X-Build-Commit`')

    const unexposed = deploymentCard(row({
      deployment: deployment({
        status: 'proposed',
        proposal: proposal({ exposure: { kind: 'none', path: null, field: null, header: null }, proposal: { title: 'Expose the deployed commit', body: '…' } }),
      }),
    }), null, NOW)!
    expect(unexposed.exposure).toBe('Not exposed — an issue is filed when you confirm')

    const filed = deploymentCard(row({
      deployment: deployment({
        status: 'confirmed', exposure_issue: 41,
        profile: profile({ exposure: { kind: 'none', path: null, field: null, header: null }, exposure_issue: 41 }),
      }),
    }), null, NOW)!
    expect(filed.exposure).toBe('Not exposed — issue #41 filed')
  })

  it('does not offer to track an unrelated domain when deployment is unobservable', () => {
    const card = deploymentCard(row({ deployment: deployment({ status: 'proposed', proposal: proposal({
      strategy: 'unobservable', domain: { value: 'backend.example.com', source: 'docs', confidence: 'high' },
      exposure: { kind: 'none', path: null, field: null, header: null }, proposal: null,
    }) }) }), null, NOW)!
    expect(card.status).toBe('Tracking unavailable')
    expect(card.unavailable).toBe(true)
    expect(card.showDomain).toBe(false)
    expect(card.actions).toEqual({ confirm: false, reject: true, reread: true })
    const historical = deploymentCard(row({ deployment: deployment({ status: 'confirmed', profile: profile({ strategy: 'unobservable' }) }) }), observed(), NOW)!
    expect(historical.status).toBe('Tracking unavailable')
    expect(historical.observation).toBe('')
  })

  it('folds a confirmed profile into one line and reads the observation beside it', () => {
    const card = deploymentCard(row({ deployment: deployment({ status: 'confirmed', profile: profile() }) }), observed(), NOW)!
    expect(card.state).toBe('confirmed')
    expect(card.status).toBe('Tracked')
    expect(card.sentence).toBe('')
    expect(card.lines).toEqual(['Health endpoint · workflow .github/workflows/deploy.yml · environment production · academy.xrpl-commons.org'])
    expect(card.observation).toBe('live commit abc1234 · deploy succeeded 12 min ago')
    expect(card.showDomain).toBe(false)
    expect(card.actions).toEqual({ confirm: false, reject: false, reread: true })
  })

  it('reports an observation failure instead of claiming a healthy deployment', () => {
    const card = deploymentCard(
      row({ deployment: deployment({ status: 'confirmed', profile: profile(), error: 'The deployment issue could not be filed; re-read deployment to retry' }) }),
      observed({ live: null, deploy: null, error: 'The deployment did not answer in time (retrying in 8 min)' }),
      NOW,
    )!
    expect(card.observation).toBe('Nothing observed yet.')
    expect(card.error).toBe('The deployment issue could not be filed; re-read deployment to retry · The deployment did not answer in time (retrying in 8 min)')
  })

  it('gives every other state one sentence and a re-read', () => {
    const idle = deploymentCard(row({ deployment: deployment() }), null, NOW)!
    expect(idle.status).toBe('Not read yet')
    expect(idle.sentence).toBe('Forge has not read how this repository deploys yet.')
    expect(idle.lines).toEqual([])
    expect(idle.actions).toEqual({ confirm: false, reject: false, reread: true })

    const running = deploymentCard(row({ deployment: deployment({ status: 'running' }) }), null, NOW)!
    expect(running.status).toBe('Reading…')
    expect(running.actions.reread).toBe(false)

    const failed = deploymentCard(row({ deployment: deployment({ status: 'failed', summary: 'No deploy workflow could be found.' }) }), null, NOW)!
    expect(failed.status).toBe('Could not read')
    expect(failed.sentence).toBe('No deploy workflow could be found.')
    expect(failed.actions.reread).toBe(true)

    const rejected = deploymentCard(row({ deployment: deployment({ status: 'rejected', reason: 'Wrong environment' }) }), null, NOW)!
    expect(rejected.status).toBe('Rejected')
    expect(rejected.sentence).toBe('Rejected · Wrong environment')

    const queued = deploymentCard(row({ deployment: deployment({ status: 'confirmed', profile: profile(), reread_requested: true }) }), null, NOW)!
    expect(queued.note).toBe('A re-read is queued for the next cycle.')
  })
})

describe('connectionCards recommendations', () => {
  it('lists a recommended permission without changing the row status', () => {
    const settings = {
      ok: true, controls_token: 'x', revision: 'r', configured: true, config_file: 'factory.json',
      config: { version: 1, sync_interval_seconds: 300, access_max_age_seconds: 900, connections: { controller: { app_id: 4, private_key_env: 'K' } }, repositories: [] },
      roles: {
        build: access(), review: access(), audit: access(),
        controller: access({ missing_recommended: ['deployments: read'], recommended_permissions: { deployments: 'read' } }),
      },
      repositories: [], sync: { status: 'complete', finished_at: 1790000000 },
    } as unknown as SettingsView
    const cards = connectionCards(settings, null)
    const controller = cards.find(card => card.role === 'controller')!
    expect(controller.ready).toBe(true)
    expect(controller.errors).toEqual([])
    expect(controller.recommendation).toBe('Recommended deployments: read')
    expect(cards.find(card => card.role === 'build')!.recommendation).toBe('')
  })
})
