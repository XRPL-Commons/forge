import { describe, expect, it } from 'vitest'
import { elapsedTime, githubLink, installationHeadline, installationPending, type InstallationView } from '../../app/utils/installation'
const base: InstallationView = { available: true, complete: false, observed_at: 100, updated_at: 80, source: 'cli', stages: [], smoke: null, operation: null, actions: [] }
describe('installation status', () => {
  it('keeps repository activation blocked until the server confirms completion', () => {
    expect(installationPending(base)).toBe(true)
    expect(installationPending({ ...base, complete: true })).toBe(false)
    expect(installationPending({ ...base, available: false })).toBe(false)
  })
  it('prioritizes a failed stage over an older pending smoke report or future steps', () => {
    expect(installationHeadline({ ...base, stages: [
      { id: 'repo', label: 'Repositories', status: 'pending' },
      { id: 'environment', label: 'Build environment', status: 'failed', error: 'Image build failed.' },
    ], smoke: { status: 'pending', reason: 'Waiting for test.' } })).toBe('Image build failed.')
  })
  it('does not imply elapsed time without a recorded start or accept non-GitHub action links', () => {
    expect(elapsedTime(undefined, 100)).toBeNull()
    expect(elapsedTime(200, 100)).toBeNull()
    expect(githubLink('javascript:alert(1)')).toBeUndefined()
    expect(githubLink('https://github.com.example.com/issues/1')).toBeUndefined()
    expect(githubLink('https://github.com/example/project/pull/2')).toBe('https://github.com/example/project/pull/2')
  })
})
