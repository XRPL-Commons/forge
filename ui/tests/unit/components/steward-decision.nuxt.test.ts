// @vitest-environment nuxt
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mountSuspended, mockNuxtImport } from '@nuxt/test-utils/runtime'
import { flushPromises } from '@vue/test-utils'
import StewardDecision from '../../../app/components/StewardDecision.vue'
import { ForgeApiError } from '../../../app/composables/useForgeApi'
import type { StewardDecision as Decision, StewardDecisionResponse } from '../../../app/utils/steward'

const { get, act } = vi.hoisted(() => ({ get: vi.fn(), act: vi.fn() }))
mockNuxtImport('useForgeApi', () => () => ({ get, act }))
let sequence = 0
function fixture(): Decision {
  return { version: 1, decision_id: `decision-${++sequence}`, digest: 'a'.repeat(64), status: 'proposed', application: null,
    recommended_choice_id: 'retry', confidence: .75, brief: null,
    choices: [{ id: 'retry', action: 'retry', reason: 'The checks need more time.', judgement: 'judgement', parameters: { timeout_seconds: 1200 } }],
  }
}
function choose(wrapper: Awaited<ReturnType<typeof mountSuspended>>) { return wrapper.get('[data-choice-id="retry"] button') }
function refresh(wrapper: Awaited<ReturnType<typeof mountSuspended>>) { return wrapper.findAll('button').find(button => button.text() === 'Refresh choices')! }

describe('steward decision submission', () => {
  beforeEach(() => { get.mockReset(); act.mockReset(); sessionStorage.clear() })

  it('loads only when visible and shares the local read cache across surfaces', async () => {
    const decision = fixture(), key = `1:${sequence}`
    get.mockResolvedValue({ ok: true, repositories: [], decision })
    const hidden = await mountSuspended(StewardDecision, { props: { issueKey: key, active: false } })
    expect(get).not.toHaveBeenCalled()
    await hidden.setProps({ active: true })
    await flushPromises()
    expect(get).toHaveBeenCalledTimes(1)
    expect(get).toHaveBeenCalledWith(`/api/issues/steward?key=${encodeURIComponent(key)}`)
    const second = await mountSuspended(StewardDecision, { props: { issueKey: key } })
    await flushPromises()
    expect(get).toHaveBeenCalledTimes(1)
    expect(second.text()).toContain('The checks need more time.')
    hidden.unmount(); second.unmount()
  })

  it('submits the exact durable choice once and separates selection from application', async () => {
    const decision = fixture(), key = `1:${sequence}`
    get.mockResolvedValue({ ok: true, repositories: [], decision })
    let resolve!: (response: StewardDecisionResponse) => void
    act.mockImplementation(() => new Promise(done => { resolve = done }))
    const first = await mountSuspended(StewardDecision, { props: { issueKey: key } })
    const second = await mountSuspended(StewardDecision, { props: { issueKey: key } })
    await flushPromises()
    await choose(first).trigger('click')
    await choose(second).trigger('click')
    expect(act).toHaveBeenCalledTimes(1)
    expect(act.mock.calls[0]).toEqual(['/api/issues/steward', { key, decision_id: decision.decision_id,
      choice_id: 'retry', digest: decision.digest, request_id: expect.any(String) }])
    expect(choose(second).attributes('disabled')).toBeDefined()
    resolve({ ok: true, repositories: [], decision: { ...decision, status: 'selected', application: {
      request_id: act.mock.calls[0]![1].request_id, choice_id: 'retry', status: 'pending', message: null,
    } } })
    await flushPromises()
    expect(first.text()).toContain('Your choice is recorded. Forge is preparing the next step.')
    expect(first.text()).not.toContain('This choice has been applied.')
    expect(choose(first).attributes('disabled')).toBeDefined()
    first.unmount(); second.unmount()
  })

  it('keeps an uncertain write locked through refresh and component remount', async () => {
    const decision = fixture(), key = `1:${sequence}`
    get.mockResolvedValue({ ok: true, repositories: [], decision })
    act.mockRejectedValue(new ForgeApiError(0, 'Connection lost'))
    let wrapper = await mountSuspended(StewardDecision, { props: { issueKey: key } })
    await flushPromises()
    await choose(wrapper).trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('The reply was interrupted.')
    const stored = JSON.parse(sessionStorage.getItem(`forge-steward:${decision.decision_id}:${decision.digest}`)!)
    expect(Object.keys(stored).sort()).toEqual(['choice_id', 'decision_id', 'digest', 'request_id', 'state'])
    await refresh(wrapper).trigger('click'); await flushPromises()
    expect(choose(wrapper).attributes('disabled')).toBeDefined()
    wrapper.unmount()
    wrapper = await mountSuspended(StewardDecision, { props: { issueKey: key } })
    await flushPromises()
    await choose(wrapper).trigger('click')
    expect(act).toHaveBeenCalledTimes(1)
    get.mockResolvedValue({ ok: true, repositories: [], decision: { ...decision, status: 'applied', application: {
      request_id: stored.request_id, choice_id: 'retry', status: 'applied', message: null,
    } } })
    await refresh(wrapper).trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('This choice has been applied.')
    expect(sessionStorage.getItem(`forge-steward:${decision.decision_id}:${decision.digest}`)).toBeNull()
    wrapper.unmount()
  })

  it('explicitly retries a lost selection with the exact original request identity', async () => {
    const decision = fixture(), key = `1:${sequence}`
    get.mockResolvedValue({ ok: true, repositories: [], decision })
    act.mockRejectedValueOnce(new ForgeApiError(0, 'Connection lost'))
    const wrapper = await mountSuspended(StewardDecision, { props: { issueKey: key } })
    await flushPromises()
    await choose(wrapper).trigger('click'); await flushPromises()
    const original = act.mock.calls[0]![1]
    await refresh(wrapper).trigger('click'); await flushPromises()
    act.mockResolvedValueOnce({ ok: true, repositories: [], decision: { ...decision, status: 'selected',
      application: { request_id: original.request_id, choice_id: 'retry', status: 'pending', message: null } } })
    const retry = wrapper.findAll('button').find(button => button.text() === 'Retry recording this choice')!
    await retry.trigger('click'); await flushPromises()
    expect(act.mock.calls[1]![1]).toEqual(original)
    expect(act).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain('Your choice is recorded.')
    expect(wrapper.text()).not.toContain('Retry recording this choice')
    wrapper.unmount()
  })

  it('restores a lost request lock from session storage after a page reload', async () => {
    const decision = fixture(), key = `1:${sequence}`
    get.mockResolvedValue({ ok: true, repositories: [], decision })
    sessionStorage.setItem(`forge-steward:${decision.decision_id}:${decision.digest}`, JSON.stringify({
      decision_id: decision.decision_id, digest: decision.digest, choice_id: 'retry', request_id: 'lost-request', state: 'submitting',
    }))
    const wrapper = await mountSuspended(StewardDecision, { props: { issueKey: key } })
    await flushPromises()
    expect(wrapper.text()).toContain('The reply was interrupted.')
    expect(choose(wrapper).attributes('disabled')).toBeDefined()
    await choose(wrapper).trigger('click')
    expect(act).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('disables stale cached choices when the board has a newer decision', async () => {
    const decision = fixture(), key = `1:${sequence}`
    get.mockResolvedValue({ ok: true, repositories: [], decision })
    const wrapper = await mountSuspended(StewardDecision, { props: { issueKey: key,
      expected: { decision_id: decision.decision_id, digest: 'b'.repeat(64), status: 'proposed' } } })
    await flushPromises()
    expect(choose(wrapper).attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('These choices have changed')
    get.mockResolvedValue({ ok: true, repositories: [], decision: { ...decision, digest: 'b'.repeat(64) } })
    await refresh(wrapper).trigger('click'); await flushPromises()
    expect(choose(wrapper).attributes('disabled')).toBeUndefined()
    await wrapper.setProps({ expected: { decision_id: decision.decision_id, digest: 'b'.repeat(64), status: 'stale' } })
    expect(choose(wrapper).attributes('disabled')).toBeDefined()
    wrapper.unmount()
  })
  it('keeps the old issue response out of a newly opened issue', async () => {
    const old = fixture(), oldKey = `1:${sequence}`
    const next = fixture(), nextKey = `1:${sequence}`
    get.mockImplementation((path: string) => Promise.resolve({ ok: true, repositories: [], decision: path.includes(encodeURIComponent(oldKey)) ? old : next }))
    let resolve!: (response: StewardDecisionResponse) => void
    act.mockImplementation(() => new Promise(done => { resolve = done }))
    const wrapper = await mountSuspended(StewardDecision, { props: { issueKey: oldKey } })
    await flushPromises()
    await choose(wrapper).trigger('click')
    await wrapper.setProps({ issueKey: nextKey }); await flushPromises()
    resolve({ ok: true, repositories: [], decision: { ...old, status: 'selected' } })
    await flushPromises()
    expect(choose(wrapper).attributes('disabled')).toBeUndefined()
    expect(wrapper.text()).not.toContain('Your choice is recorded.')
    await choose(wrapper).trigger('click')
    expect(act.mock.calls[1]![1]).toMatchObject({ key: nextKey, decision_id: next.decision_id, digest: next.digest })
    resolve({ ok: true, repositories: [], decision: { ...next, status: 'selected' } }); await flushPromises()
    wrapper.unmount()
  })

  it('requires a refreshed decision after a conflict and never replays the rejected choice', async () => {
    const decision = fixture(), key = `1:${sequence}`
    get.mockResolvedValue({ ok: true, repositories: [], decision })
    act.mockRejectedValue(new ForgeApiError(409, 'The decision changed.'))
    const wrapper = await mountSuspended(StewardDecision, { props: { issueKey: key } })
    await flushPromises()
    get.mockResolvedValue({ ok: true, repositories: [], decision: { ...decision, status: 'stale' } })
    await choose(wrapper).trigger('click'); await flushPromises()
    expect(act).toHaveBeenCalledTimes(1)
    expect(choose(wrapper).attributes('disabled')).toBeDefined()
    await refresh(wrapper).trigger('click'); await flushPromises()
    expect(act).toHaveBeenCalledTimes(1)
    expect(choose(wrapper).attributes('disabled')).toBeDefined()
    wrapper.unmount()
  })

})
