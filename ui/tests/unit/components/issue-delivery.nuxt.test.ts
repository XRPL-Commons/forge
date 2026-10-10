// @vitest-environment nuxt
import { beforeEach, describe, it, expect, vi } from 'vitest'
import { mountSuspended, mockNuxtImport } from '@nuxt/test-utils/runtime'
import { flushPromises } from '@vue/test-utils'
import IssueDelivery from '../../../app/components/IssueDelivery.vue'

const { get } = vi.hoisted(() => ({ get: vi.fn() }))
mockNuxtImport('useForgeApi', () => () => ({ get }))

const output = { member_id: 'one', output_id: 'output-one', commit_oid: 'abcdef1234567890', input_manifest_hash: 'inputs' }
const hold = { id: 'hold', code: 'label_hold', reason: 'On hold by a GitHub label; removing it clears this hold', member_id: 'one', candidate_id: null, subject_key: 'member:one' }
const environment = { id: 'environment', code: 'environment', reason: 'Waiting for the build environment to be repaired', member_id: 'one', candidate_id: null, subject_key: 'member:one' }
function member(id: string, number: number, title: string, code: string, label: string, reason: string) {
  return { id, issue_key: `1:${number}`, number, title, operator_status: { code, label, reason_code: null, reason }, gates: [], output: null, dependencies: [] }
}
describe('combined delivery', () => {
  beforeEach(() => get.mockReset())
  it('loads scoped progress only when opened, with separate child output links', async () => {
    get.mockResolvedValue({ unit_id: 'batch', version: 2, state: 'building', builder_id: 'owner', repo: 'example/repo',
      candidate_pr: null, charged_failures: 1, failure_limit: 3, next_cursor: null,
      members: [
        { ...member('one', 11, 'First scope', 'blocked', 'Blocked', hold.reason), output, gates: [hold, environment] },
        { ...member('two', 12, 'Second scope', 'working', 'Working', 'Building'), dependencies: [{ member_id: 'one', issue_key: '1:11', number: 11, output }] },
        { ...member('three', 13, 'Dependent scope', 'blocked', 'Blocked', 'Waiting for accepted prerequisite outputs'),
          gates: [{ id: null, code: 'dependency', reason: 'Waiting for accepted prerequisite outputs', member_id: 'three', candidate_id: null, subject_key: 'member:three' }],
          dependencies: [{ member_id: 'two', issue_key: '1:12', number: 12, output: null }] },
      ],
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
    const members = wrapper.get('[aria-label="Delivery members"]').findAll(':scope > li')
    expect(members[0]!.text()).toContain('Blocked')
    expect(members[0]!.text()).toContain('Output accepted')
    expect(members[0]!.get('[aria-label="Blockers for issue 11"]').findAll('li').map(row => row.text())).toEqual([hold.reason, environment.reason])
    expect(members[0]!.get('a[href="https://github.com/example/repo/commit/abcdef1234567890"]').text()).toContain('abcdef1')
    expect(members[1]!.text()).toContain('Working')
    expect(members[1]!.find('[aria-label="Blockers for issue 12"]').exists()).toBe(false)
    expect(members[1]!.get('[aria-label="Prerequisites for issue 12"]').text()).toContain('Output accepted')
    expect(members[2]!.get('[aria-label="Blockers for issue 13"]').text()).toBe('Waiting for accepted prerequisite outputs')
    expect(members[2]!.get('[aria-label="Prerequisites for issue 13"]').text()).toContain('Waiting for output')
    expect(members[2]!.get('[aria-label="Prerequisites for issue 13"] a').attributes('href')).toBe('/issues/1%3A12')
    expect(wrapper.findAll('a').some(link => link.attributes('href') === '/issues/1%3A12?job=child')).toBe(true)
    expect(get.mock.calls.every(call => !String(call[0]).includes('output'))).toBe(true)
    wrapper.unmount()
  })
})
