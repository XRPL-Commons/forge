import { describe, expect, it } from 'vitest'
import { accountNeedsOperator, codexDeviceLink, elapsedTime, githubLink, installationHeadline, installationPending, setupPath, stageStatus, type InstallationView } from '../../app/utils/installation'
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
  it('distinguishes operator consent and repository decisions from running work', () => {
    const account = { ...base, actions: [{ id: 'connect_account' as const, label: 'Connect Codex' }] }
    expect(accountNeedsOperator(account)).toBe(true)
    expect(installationHeadline(account)).toBe('Waiting for you to connect Codex')
    expect(installationHeadline({ ...base, preparation: { status: 'waiting', stages: [], providers: [], ready: false,
      needs_operator: [{ repository: { id: 1, name: 'owner/project' }, reason: 'Rules need updating', path: '/settings/repositories' }] } })).toBe('Waiting for you to finish repository setup')
    expect(stageStatus('needs_operator')).toBe('Waiting for you')
    expect(stageStatus('running')).toBe('In progress')
  })
  it('uses only the device sign-in origin and local setup links', () => {
    expect(codexDeviceLink('https://auth.openai.com/codex/device')).toBe('https://auth.openai.com/codex/device')
    expect(codexDeviceLink('https://auth.openai.com.example.test/codex/device')).toBeUndefined()
    expect(codexDeviceLink('https://user@auth.openai.com/codex/device')).toBeUndefined()
    expect(codexDeviceLink('javascript:alert(1)')).toBeUndefined()
    expect(setupPath('/settings/repositories?repo=1')).toBe('/settings/repositories?repo=1')
    expect(setupPath('//outside.example')).toBeUndefined()
    expect(setupPath('https://outside.example')).toBeUndefined()
  })

})
