// @vitest-environment nuxt
import { describe, it, expect, vi } from 'vitest'
import { mountSuspended, mockNuxtImport } from '@nuxt/test-utils/runtime'
import { flushPromises } from '@vue/test-utils'
import IssueDelivery from '../../../app/components/IssueDelivery.vue'

const { get } = vi.hoisted(() => ({ get: vi.fn() }))
mockNuxtImport('useForgeApi', () => () => ({ get }))

describe('combined delivery', () => {
  it('loads scoped progress only when opened, with separate child output links', async () => {
    get.mockResolvedValue({ unit_id: 'batch', version: 2, state: 'building', builder_id: 'owner', repo: 'example/repo',
      candidate_pr: null, charged_failures: 1, failure_limit: 3, next_cursor: null,
      members: [{ id: 'one', issue_key: '1:11', number: 11, title: 'First scope', state: 'built', output_commit: 'a' },
        { id: 'two', issue_key: '1:12', number: 12, title: 'Second scope', state: 'building', output_commit: null }],
      jobs: [{ id: 'parent', role: 'build', phase: 'cleaned', outcome: 'planned', builder_stage: 'planning', issue_key: null, members: [{}, {}] },
        { id: 'child', role: 'build', phase: 'running', outcome: null, builder_stage: 'child', parent_job_id: 'parent', issue_key: '1:12', issue_number: 12, delegation_key: 'two' }],
    })
    const wrapper = await mountSuspended(IssueDelivery, { props: { issueKey: '1:11' } })
    expect(get).not.toHaveBeenCalled()
    await wrapper.findAll('button').find(button => button.text().includes('Combined delivery'))!.trigger('click')
    await flushPromises()
    expect(get).toHaveBeenCalledWith('/api/delivery?issue=1%3A11&after=')
    expect(wrapper.text()).toContain('1 / 3 shared failures')
    expect(wrapper.text()).toContain('Builder planning')
    expect(wrapper.text()).toContain('Child implementation')
    expect(wrapper.findAll('a').some(link => link.attributes('href') === '/issues/1%3A12?job=child')).toBe(true)
    expect(get.mock.calls.every(call => !String(call[0]).includes('output'))).toBe(true)
    wrapper.unmount()
  })
})
