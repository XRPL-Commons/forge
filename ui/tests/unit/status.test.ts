import { describe, it, expect } from 'vitest'
import { statusRows, statusSummary } from '../../app/utils/status'

const now = 1_000_000

function factory(over: Record<string, unknown> = {}): any {
  return {
    ok: true, controls_token: 'synthetic', active: true,
    operator: { title: 'Building issue #601', detail: 'Publish build logs', action_url: null, action_label: null },
    repositories: [],
    observation: { at: now - 10, errors: [], capacity: null, issues: 0 },
    actions: [], issues: [], jobs: [], slots: {},
    runtime: {
      enabled: true, slots: { build: 2, review: 1, audit: 1, recovery: 1 }, cycle_seconds: 30,
      image: 'forge/worker:2026.09',
    },
    runtime_revision: 'rev', ...over,
  }
}

function onboarding(over: Record<string, unknown> = {}): any {
  return {
    ok: true, controls_token: 'synthetic', phase: 'active', generation: 'gen-1', revision: 'rev-1',
    harnesses: [], qualification_fresh: true, image_verified: true, error: null, checks: {}, ...over,
  }
}

function settings(over: Record<string, unknown> = {}): any {
  return {
    ok: true, controls_token: 'synthetic',
    roles: {
      build: { status: 'ok' }, review: { status: 'ok' }, audit: { status: 'ok' }, controller: { status: 'ok' },
    },
    ...over,
  }
}

function row(rows: ReturnType<typeof statusRows>, id: string) {
  const found = rows.find(r => r.id === id)
  if (!found) throw new Error(`no row ${id}`)
  return found
}

describe('statusRows · dispatch', () => {
  it('reads On/ok from a running factory', () => {
    const rows = statusRows({ factory: factory(), settings: null, onboarding: null }, now)
    expect(row(rows, 'dispatch').value).toBe('On')
    expect(row(rows, 'dispatch').tone).toBe('ok')
    expect(row(rows, 'dispatch').to).toBe('/settings#worker-capacity')
  })

  it('reports Paused with the operator detail and a warn tone', () => {
    const paused = factory({
      runtime: { enabled: false, slots: { build: 2, review: 1, audit: 1, recovery: 1 }, cycle_seconds: 30, image: 'forge/worker:2026.09' },
      operator: { title: 'Forge is paused', detail: 'Issue processing is disabled. Existing worker cleanup continues.', action_url: '/settings', action_label: 'Review settings' },
    })
    const rows = statusRows({ factory: paused, settings: null, onboarding: null }, now)
    const dispatch = row(rows, 'dispatch')
    expect(dispatch.value).toBe('Paused: no new work starts')
    expect(dispatch.tone).toBe('warn')
    expect(dispatch.detail).toBe('Issue processing is disabled. Existing worker cleanup continues.')
  })
})

describe('statusRows · image', () => {
  it('reports the image verified when the last check covered it', () => {
    const rows = statusRows({ factory: factory(), settings: null, onboarding: onboarding({ image_verified: true }) }, now)
    const image = row(rows, 'image')
    expect(image.value).toBe('Checked and ready')
    expect(image.tone).toBe('ok')
    expect(image.value).not.toContain('sha256')
  })

  it('asks for the account check, tone warn, when the image changed since the last check', () => {
    const rows = statusRows({ factory: factory(), settings: null, onboarding: onboarding({ image_verified: false }) }, now)
    const image = row(rows, 'image')
    expect(image.value).toContain('run the account check')
    expect(image.tone).toBe('warn')
    expect(image.to).toBe('/settings/harnesses')
  })
})

describe('statusRows · accounts', () => {
  it('reads all signed in when every model passed', () => {
    const rows = statusRows({ factory: null, settings: null, onboarding: onboarding({
      harnesses: [
        { id: 'codex', name: 'Codex', version: '1', connected: true, models: [{ id: 'codex', model: 'gpt', status: 'passed' }], login_command: '', check_command: '' },
        { id: 'claude', name: 'Claude Code', version: '1', connected: true, models: [{ id: 'claude', model: 'sonnet', status: 'passed' }], login_command: '', check_command: '' },
        { id: 'grok', name: 'Grok', version: '1', connected: false, models: [], login_command: '', check_command: '' },
      ],
    }) }, now)
    const accounts = row(rows, 'accounts')
    expect(accounts.value).toBe('All signed in and working')
    expect(accounts.tone).toBe('ok')
  })

  it('names the failed harness with a warn tone and no detail', () => {
    const rows = statusRows({ factory: null, settings: null, onboarding: onboarding({
      harnesses: [
        { id: 'claude', name: 'Claude Code', version: '1', connected: true, models: [{ id: 'claude', model: 'sonnet', status: 'failed' }], login_command: '', check_command: '' },
      ],
    }) }, now)
    const accounts = row(rows, 'accounts')
    expect(accounts.value).toBe('Claude Code failed')
    expect(accounts.tone).toBe('warn')
    expect(accounts.detail).toBeUndefined()
  })

  it('is muted, not warn, when the only non-passed status is a self-healing usage limit', () => {
    const rows = statusRows({ factory: null, settings: null, onboarding: onboarding({
      harnesses: [
        { id: 'codex', name: 'Codex', version: '1', connected: true, models: [{ id: 'codex', model: 'gpt', status: 'usage limit; retry pending' }], login_command: '', check_command: '' },
      ],
    }) }, now)
    const accounts = row(rows, 'accounts')
    expect(accounts.value).toBe('Codex usage limit; retry pending')
    expect(accounts.tone).toBe('muted')
  })

  it('warns on verification required alongside a passed account', () => {
    const rows = statusRows({ factory: null, settings: null, onboarding: onboarding({
      harnesses: [
        { id: 'codex', name: 'Codex', version: '1', connected: true, models: [{ id: 'codex', model: 'gpt', status: 'passed' }], login_command: '', check_command: '' },
        { id: 'claude', name: 'Claude Code', version: '1', connected: true, models: [{ id: 'claude', model: 'sonnet', status: 'verification required' }], login_command: '', check_command: '' },
      ],
    }) }, now)
    const accounts = row(rows, 'accounts')
    expect(accounts.value).toBe('Codex passed · Claude Code verification required')
    expect(accounts.tone).toBe('warn')
  })
})

describe('statusRows · github', () => {
  it('counts ok roles out of the total', () => {
    const rows = statusRows({ factory: null, settings: settings(), onboarding: null }, now)
    const github = row(rows, 'github')
    expect(github.value).toBe('All Forge Apps connected')
    expect(github.tone).toBe('ok')
  })

  it('warns when fewer than all roles are ok', () => {
    const rows = statusRows({ factory: null, settings: settings({
      roles: { build: { status: 'ok' }, review: { status: 'stale' }, audit: { status: 'ok' }, controller: { status: 'ok' } },
    }), onboarding: null }, now)
    const github = row(rows, 'github')
    expect(github.value).toBe('3 of 4 Forge Apps connected')
    expect(github.tone).toBe('warn')
  })
})

describe('statusRows · observation', () => {
  it('reports a fresh observation age with an ok tone', () => {
    const rows = statusRows({ factory: factory({ observation: { at: now - 10, errors: [], capacity: null, issues: 0 } }), settings: null, onboarding: null }, now)
    const observation = row(rows, 'observation')
    expect(observation.value).toBe('Running · checked just now')
    expect(observation.tone).toBe('ok')
  })

  it('warns when the observation is older than maxAge', () => {
    const rows = statusRows({ factory: factory({ observation: { at: now - 1000, errors: [], capacity: null, issues: 0 } }), settings: null, onboarding: null }, now)
    expect(row(rows, 'observation').tone).toBe('warn')
  })

  it('reads Not seen yet when there is none', () => {
    const rows = statusRows({ factory: factory({ observation: {} }), settings: null, onboarding: null }, now)
    expect(row(rows, 'observation').value).toBe('Not seen yet')
  })
})

describe('statusRows · missing sources', () => {
  it('renders every row as Not available, muted, with no link when all three sources are missing', () => {
    const rows = statusRows({ factory: null, settings: null, onboarding: null }, now)
    expect(rows).toHaveLength(5)
    for (const entry of rows) {
      expect(entry.value).toBe('Not available')
      expect(entry.tone).toBe('muted')
      expect(entry.to).toBeUndefined()
    }
  })
})

describe('statusSummary', () => {
  const healthy = () => statusRows({ factory: factory({ observation: { at: now - 10, errors: [], capacity: null, issues: 0 } }),
    settings: settings(), onboarding: onboarding({ image_verified: true, harnesses: [
      { id: 'codex', name: 'Codex', version: '1', connected: true, models: [{ id: 'codex', model: 'gpt', status: 'passed' }], login_command: '', check_command: '' }] }) }, now)

  it('says one sentence and lists nothing when everything is healthy', () => {
    const summary = statusSummary(healthy())
    expect(summary).toMatchObject({ tone: 'ok', headline: 'Forge is running. Workers can start new work.', attention: [] })
  })

  it('lists only the rows that need the operator', () => {
    const rows = healthy().map(r => r.id === 'github' ? { ...r, tone: 'warn' as const, value: '3 of 4 Forge Apps connected' } : r)
    const summary = statusSummary(rows)
    expect(summary.headline).toBe('One thing needs your attention.')
    expect(summary.attention.map(r => r.id)).toEqual(['github'])
  })

  it('reads paused when dispatch is the only issue', () => {
    const rows = healthy().map(r => r.id === 'dispatch' ? { ...r, tone: 'warn' as const } : r)
    expect(statusSummary(rows).headline).toBe('Forge is paused. No new work starts.')
  })

  it('does not claim a problem before any data arrived', () => {
    const summary = statusSummary(statusRows({ factory: null, settings: null, onboarding: null }, now))
    expect(summary).toMatchObject({ tone: 'muted', attention: [] })
  })
})
