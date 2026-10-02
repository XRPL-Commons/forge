import { describe, it, expect } from 'vitest'
import { summarizeActivity, ROLE_TARGET } from '../../app/utils/activity'
const now = 1_000_000
const payload = { ok: true, active: true, observation: { at: now - 10 }, runtime: { cycle_seconds: 30, enabled: true },
  jobs: [ { role: 'build', phase: 'running', observed_at: now - 5 }, { role: 'recovery', phase: 'running', observed_at: now - 5 },
          { role: 'review', phase: 'running', observed_at: now - 1000 }, { role: 'audit', phase: 'running', cleanup: true, observed_at: now - 5 } ],
  issues: [ { state: 'ready', active: null, repo: 'a/b' }, { state: 'built', active: 'j', repo: 'a/b' }, { state: 'ready', active: null, repo: 'a/off' } ],
  repositories: [ { name: 'a/b', enabled: true }, { name: 'a/off', enabled: false } ] }
describe('summarizeActivity', () => {
  it('counts fresh running workers per role, recovery included, and queued issues of enabled repos', () => {
    expect(summarizeActivity(payload as any, now)).toEqual({ working: { build: 1, review: 0, audit: 0, merge: 0, recovery: 1 }, queued: 1, fresh: true })
  })
  it('reports stale when the observation is older than three cycles', () => {
    expect(summarizeActivity({ ...payload, observation: { at: now - 200 } } as any, now).fresh).toBe(false)
  })
  it('treats a non-array repositories as all repos enabled and counts all queued issues', () => {
    expect(summarizeActivity({ ...payload, repositories: null } as any, now).queued).toBe(2)
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
