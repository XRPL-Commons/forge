import { describe, it, expect } from 'vitest'
import { summarizeActivity, ROLE_TARGET } from '../../app/utils/activity'
import { factoryRow } from './support/rows'
const now = 1_000_000
const running = { phase: 'running', observed_status: 'running', observed_at: now - 5, cleanup: false, fenced: false, outcome: null }
const payload = { ok: true, active: true, observation: { at: now - 10 }, runtime: { cycle_seconds: 30, enabled: true },
  jobs: [ { ...running, id: 'b', role: 'build' }, { ...running, id: 'r', role: 'recovery' },
          { ...running, id: 'v', role: 'review', observed_at: now - 1000 }, { ...running, id: 'a', role: 'audit', cleanup: true },
          { ...running, id: 'm', role: 'merge', observed_status: 'exited' } ],
  issues: [ factoryRow({ key: '1', state: 'ready', active: null, repo_id: 1, repo: 'a/b' }), factoryRow({ key: '2', state: 'built', active: 'b', repo_id: 1, repo: 'a/b' }),
            factoryRow({ key: '3', state: 'ready', active: null, repo_id: 2, repo: 'a/off' }), factoryRow({ key: '4', state: 'ready', active: null, repo_id: 1, repo: 'a/b', blocked_dependencies: [9] }) ],
  repositories: [ { id: 1, name: 'a/b', enabled: true }, { id: 2, name: 'a/off', enabled: false } ] }
describe('summarizeActivity', () => {
  it('counts running workers per role as the Activity page does, and queued issues of enabled repos', () => {
    expect(summarizeActivity(payload as any, now)).toEqual({ working: { build: 1, review: 0, audit: 0, merge: 0, recovery: 1 }, queued: 1, fresh: true })
  })
  it('reports stale, and nobody working, when the observation is older than three cycles', () => {
    const stale = summarizeActivity({ ...payload, observation: { at: now - 200 } } as any, now)
    expect(stale.fresh).toBe(false)
    expect(Object.values(stale.working).every(count => count === 0)).toBe(true)
  })
  it('treats a non-array repositories as all repos enabled and counts all queued issues', () => {
    expect(summarizeActivity({ ...payload, repositories: null } as any, now).queued).toBe(2)
  })
  it('reads nothing from no view', () => {
    expect(summarizeActivity(null, now)).toEqual({ working: { build: 0, review: 0, audit: 0, merge: 0, recovery: 0 }, queued: 0, fresh: false })
  })
})

describe('ROLE_TARGET', () => {
  it('sends the working roles to the factory line, which draws every stage', () => {
    for (const role of ['build', 'review', 'audit', 'merge'] as const) expect(ROLE_TARGET[role]).toBe('/#factory-line')
    expect(ROLE_TARGET.recovery).toBe('/#runners')
  })
  it('points every role at a section the Activity page renders', async () => {
    const { readFileSync } = await import('node:fs')
    const page = readFileSync(`${process.cwd()}/app/pages/index.vue`, 'utf8')
    for (const target of Object.values(ROLE_TARGET)) {
      const id = target.split('#')[1]
      expect(page.includes(`id="${id}"`), `${id} is rendered`).toBe(true)
    }
  })
})
