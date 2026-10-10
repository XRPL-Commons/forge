// @vitest-environment nuxt
import { beforeEach, describe, it, expect, vi } from 'vitest'
import { mountSuspended, mockNuxtImport } from '@nuxt/test-utils/runtime'
import { flushPromises } from '@vue/test-utils'
import IssueDelivery from '../../../app/components/IssueDelivery.vue'
import { ForgeApiError } from '../../../app/composables/useForgeApi'

const { get } = vi.hoisted(() => ({ get: vi.fn() }))
mockNuxtImport('useForgeApi', () => () => ({ get }))

const output = { task_id: 'task-one', attempt_id: 'attempt-one', output_id: 'output-one', commit_oid: 'abcdef1234567890', input_manifest_hash: 'inputs' }
const hold = { id: 'hold', code: 'label_hold', reason: 'On hold by a GitHub label; removing it clears this hold', member_id: 'one', candidate_id: null, subject_key: 'member:one' }
const environment = { id: 'environment', code: 'environment', reason: 'Waiting for the build environment to be repaired', member_id: 'one', candidate_id: null, subject_key: 'member:one' }
function member(id: string, number: number, title: string, code: string, label: string, reason: string) {
  return { id, issue_key: `1:${number}`, number, title, operator_status: { code, label, reason_code: null, reason }, gates: [], contributions: contributions(id), dependencies: [] }
}
function contributions(id: string, outputs: typeof output[] = [], taskIds: string[] = ['task-one']) {
  return { plan_id: 'plan', member_id: id, candidate_id: null, task_ids: taskIds, outputs,
    ready: outputs.length === taskIds.length, owner_final_pending: false }
}
const plan = { id: 'plan', version: 2, owner_final: { member_ids: [], scope: '' } }
const task = { id: 'task-one', key: 'shared-model', executor: 'child', scope: 'Implement the shared model',
  members: [{ member_id: 'one', requirements_revision_id: 'revision-one' }, { member_id: 'two', requirements_revision_id: 'revision-two' }],
  dependencies: [], output, status: 'accepted', pending_reason: null, active_job_ids: [] }
describe('combined delivery', () => {
  beforeEach(() => { get.mockReset() })
  it('loads scoped progress only when opened, with separate child output links', async () => {
    get.mockResolvedValue({ unit_id: 'batch', version: 2, state: 'building', builder_id: 'owner', repo: 'example/repo',
      candidate_pr: null, charged_failures: 1, failure_limit: 3, next_cursor: null,
      response_version: 2, plan, tasks: [task], task_next_cursor: null, tasks_complete: true,
      members: [
        { ...member('one', 11, 'First scope', 'blocked', 'Blocked', hold.reason), contributions: contributions('one', [output]), gates: [hold, environment] },
        { ...member('two', 12, 'Second scope', 'working', 'Working', 'Building'), dependencies: [{ member_id: 'one', issue_key: '1:11', number: 11, contributions: contributions('one', [output]) }] },
        { ...member('three', 13, 'Dependent scope', 'blocked', 'Blocked', 'Waiting for accepted prerequisite outputs'),
          gates: [{ id: null, code: 'dependency', reason: 'Waiting for accepted prerequisite outputs', member_id: 'three', candidate_id: null, subject_key: 'member:three' }],
          dependencies: [{ member_id: 'two', issue_key: '1:12', number: 12, contributions: contributions('two') }] },
      ],
      jobs: [{ id: 'parent', role: 'build', phase: 'cleaned', outcome: 'planned', builder_stage: 'planning', issue_key: null, members: [{}, {}], resource_usage: {
        captured_at: 210, wall: { seconds: 55, started_at: 150, finished_at: 205, provenance: 'docker_inspect', unavailable_reason: null },
        sample: { sampled_at: 190, cpu_seconds: 2.5, memory_bytes: 1048576, provenance: 'docker_engine_stats' },
        collection: { checked_at: 209, status: 'unavailable', reason: 'container_not_running' },
      } },
        { id: 'child', role: 'build', phase: 'running', outcome: null, builder_stage: 'task', task_executor: 'child', task_id: 'task-one', task_key: 'shared-model', parent_job_id: 'parent', issue_key: null, issue_number: null, members: [{ issue_number: 11 }, { issue_number: 12 }] }],
    })
    const wrapper = await mountSuspended(IssueDelivery, { props: { issueKey: '1:11' } })
    expect(get).not.toHaveBeenCalled()
    await wrapper.findAll('button').find(button => button.text().includes('Combined delivery'))!.trigger('click')
    await flushPromises()
    expect(get).toHaveBeenCalledWith('/api/delivery?issue=1%3A11&after=&task_after=')
    expect(wrapper.text()).toContain('1 / 3 shared failures')
    expect(wrapper.text()).toContain('Builder planning')
    expect(wrapper.get('[aria-label="Measured execution resources"]').text()).toContain('CPU 2.5 s cumulative')
    expect(wrapper.text()).toContain('Child implementation')
    const members = wrapper.get('[aria-label="Delivery members"]').findAll(':scope > li')
    expect(members[0]!.text()).toContain('Blocked')
    expect(members[0]!.text()).toContain('1 / 1 task outputs accepted')
    expect(members[0]!.get('[aria-label="Blockers for issue 11"]').findAll('li').map(row => row.text())).toEqual([hold.reason, environment.reason])
    expect(members[0]!.get('a[href="https://github.com/example/repo/commit/abcdef1234567890"]').text()).toContain('abcdef1')
    expect(members[1]!.text()).toContain('Working')
    expect(members[1]!.find('[aria-label="Blockers for issue 12"]').exists()).toBe(false)
    expect(members[1]!.get('[aria-label="Prerequisites for issue 12"]').text()).toContain('Contributions ready')
    expect(members[2]!.get('[aria-label="Blockers for issue 13"]').text()).toBe('Waiting for accepted prerequisite outputs')
    expect(members[2]!.get('[aria-label="Prerequisites for issue 13"]').text()).toContain('Waiting for contributions')
    expect(members[2]!.get('[aria-label="Prerequisites for issue 13"] a').attributes('href')).toBe('/issues/1%3A12')
    expect(wrapper.findAll('a').some(link => link.attributes('href') === '/issues/1%3A11?job=child')).toBe(true)
    expect(wrapper.get('[aria-label="Delivery tasks"]').text()).toContain('#11, #12')
    expect(wrapper.get('[aria-label="Delivery executions"]').findAll(':scope > li')).toHaveLength(2)
    expect(get.mock.calls.every(call => !String(call[0]).includes('output'))).toBe(true)
    wrapper.unmount()
  })
  it('keeps a derived dependency blocker when its persisted counterpart resolves', async () => {
    const derived = { id: null, code: 'dependency', reason: 'Waiting for other issues to close', member_id: 'dependent', candidate_id: null, subject_key: 'member:dependent' }
    const persisted = { ...derived, id: 'dependency-check' }
    const detail = {
      unit_id: 'dropped-prerequisite', version: 3, state: 'building', builder_id: 'owner', repo: 'example/repo',
      candidate_pr: null, charged_failures: 0, failure_limit: 3, next_cursor: null, jobs: [],
      response_version: 2, plan, tasks: [], task_next_cursor: null, tasks_complete: true,
      members: [{ ...member('dependent', 21, 'Dependent scope', 'blocked', 'Blocked', derived.reason), gates: [persisted, derived] }],
    }
    get.mockResolvedValue(detail)
    const wrapper = await mountSuspended(IssueDelivery, { props: { issueKey: '1:21' } })
    try {
      await wrapper.findAll('button').find(button => button.text().includes('Combined delivery'))!.trigger('click')
      await flushPromises()
      const blockers = () => wrapper.get('[aria-label="Blockers for issue 21"]').findAll('li')
      expect(blockers().map(row => row.text())).toEqual([persisted.reason, derived.reason])
      const derivedElement = blockers()[1]!.element
      get.mockResolvedValue({ ...detail, version: 4, members: [{ ...detail.members[0], gates: [derived] }] })
      // A visibility wake refreshes the existing resource after its 100 ms
      // coalescing window, without remounting the member or its gate list.
      await new Promise(resolve => setTimeout(resolve, 150))
      document.dispatchEvent(new Event('visibilitychange'))
      await flushPromises()
      expect(get).toHaveBeenCalledTimes(2)
      expect(blockers().map(row => row.text())).toEqual([derived.reason])
      expect(blockers()[0]!.element).toBe(derivedElement)
      expect(wrapper.get('[aria-label="Delivery members"]').text()).toContain('Blocked')
    } finally {
      wrapper.unmount()
    }
  })

  it('shows all contributions to one issue and keeps task paging separate from run history', async () => {
    const second = { ...output, task_id: 'task-two', output_id: 'output-two', commit_oid: '1234567890abcdef' }
    const detail = {
      response_version: 2, unit_id: 'split', version: 4, state: 'building', builder_id: 'owner', repo: 'example/repo',
      plan, candidate_pr: null, charged_failures: 0, failure_limit: 3, next_cursor: 'more-jobs', jobs: [],
      members: [{ ...member('one', 11, 'One issue', 'working', 'Working', 'Building'),
        contributions: contributions('one', [output, second], ['task-one', 'task-two']) }],
      tasks: [{ ...task, members: [task.members[0]] }], task_next_cursor: 'more-tasks', tasks_complete: false,
    }
    get.mockResolvedValue(detail)
    const wrapper = await mountSuspended(IssueDelivery, { props: { issueKey: '1:11' } })
    try {
      await wrapper.findAll('button').find(button => button.text().includes('Combined delivery'))!.trigger('click')
      await flushPromises()
      expect(wrapper.get('[aria-label="Accepted outputs for issue 11"]').findAll('a')).toHaveLength(2)
      expect(wrapper.text()).toContain('2 / 2 task outputs accepted')
      get.mockResolvedValue({ ...detail, tasks: [{ ...task, id: 'task-two', key: 'tests', executor: 'owner', members: [task.members[0]],
        dependencies: ['task-one'], output: second }], task_next_cursor: null, tasks_complete: true })
      await wrapper.findAll('button').find(button => button.text() === 'More tasks')!.trigger('click')
      await flushPromises()
      expect(get).toHaveBeenLastCalledWith('/api/delivery?issue=1%3A11&after=&task_after=more-tasks')
      expect(wrapper.get('[aria-label="Delivery tasks"]').text()).toContain('tests · Builder')
      expect(wrapper.get('[aria-label="Delivery tasks"]').text()).toContain('Task on another page')
      expect(wrapper.get('[aria-label="Accepted outputs for issue 11"]').findAll('a')).toHaveLength(2)
      expect(wrapper.findAll('button').some(button => button.text() === 'More runs')).toBe(true)
    } finally { wrapper.unmount() }
  })

  it('makes remaining owner work explicit for a delivery without delegated tasks', async () => {
    get.mockResolvedValue({ response_version: 2, unit_id: 'owner-only', version: 2, state: 'building', builder_id: 'owner', repo: 'example/repo',
      plan: { ...plan, owner_final: { member_ids: ['one'], scope: 'Implement and verify the whole change' } },
      candidate_pr: null, charged_failures: 0, failure_limit: 3, next_cursor: null, jobs: [],
      tasks: [], task_next_cursor: null, tasks_complete: true,
      members: [{ ...member('one', 51, 'One issue', 'working', 'Working', 'Building'),
        contributions: { ...contributions('one', [], []), ready: false, owner_final_pending: true } }],
    })
    const wrapper = await mountSuspended(IssueDelivery, { props: { issueKey: '1:51' } })
    try {
      await wrapper.findAll('button').find(button => button.text().includes('Combined delivery'))!.trigger('click')
      await flushPromises()
      expect(wrapper.text()).toContain('Builder has remaining integration work')
      expect(wrapper.text()).toContain('The builder will implement this delivery during integration.')
      expect(wrapper.text()).toContain('Implement and verify the whole change')
      expect(wrapper.find('[aria-label="Task pages"]').exists()).toBe(false)
    } finally { wrapper.unmount() }
  })

  it('does not let an old plan error reset another issue task page', async () => {
    const detail = { response_version: 2, unit_id: 'batch', version: 2, state: 'building', builder_id: 'owner', repo: 'example/repo',
      plan, candidate_pr: null, charged_failures: 0, failure_limit: 3, next_cursor: null, jobs: [],
      tasks: [task], task_next_cursor: 'next-tasks', tasks_complete: false, members: [] }
    let rejectOld!: (error: Error) => void
    get.mockImplementation((path: string) => path.includes('issue=1%3A61') && path.endsWith('task_after=next-tasks')
      ? new Promise((_, reject) => { rejectOld = reject }) : Promise.resolve(detail))
    const wrapper = await mountSuspended(IssueDelivery, { props: { issueKey: '1:61' } })
    try {
      await wrapper.findAll('button').find(button => button.text().includes('Combined delivery'))!.trigger('click')
      await flushPromises()
      await wrapper.findAll('button').find(button => button.text() === 'More tasks')!.trigger('click')
      await flushPromises()
      await wrapper.setProps({ issueKey: '1:62' })
      await flushPromises()
      await wrapper.findAll('button').find(button => button.text() === 'More tasks')!.trigger('click')
      await flushPromises()
      const calls = get.mock.calls.length
      rejectOld(new ForgeApiError(409, 'The delivery plan changed. Reload its tasks.'))
      await flushPromises()
      expect(get).toHaveBeenCalledTimes(calls)
      expect(get).toHaveBeenLastCalledWith('/api/delivery?issue=1%3A62&after=&task_after=next-tasks')
      expect(wrapper.findAll('button').some(button => button.text() === 'Previous tasks')).toBe(true)
    } finally { wrapper.unmount() }
  })

  it('loads a fresh first task page when the current plan cursor expires', async () => {
    const detail = { response_version: 2, unit_id: 'batch', version: 2, state: 'building', builder_id: 'owner', repo: 'example/repo',
      plan, candidate_pr: null, charged_failures: 0, failure_limit: 3, next_cursor: null, jobs: [],
      tasks: [task], task_next_cursor: 'old-page', tasks_complete: false, members: [] }
    let firstPageReads = 0
    get.mockImplementation((path: string) => {
      if (path.endsWith('task_after=old-page')) return Promise.reject(new ForgeApiError(409, 'The delivery plan changed. Reload its tasks.'))
      firstPageReads++
      return Promise.resolve(firstPageReads === 1 ? detail : { ...detail, plan: { ...plan, id: 'replacement' },
        tasks: [{ ...task, key: 'revised-task' }], task_next_cursor: null, tasks_complete: true })
    })
    const wrapper = await mountSuspended(IssueDelivery, { props: { issueKey: '1:71' } })
    try {
      await wrapper.findAll('button').find(button => button.text().includes('Combined delivery'))!.trigger('click')
      await flushPromises()
      await wrapper.findAll('button').find(button => button.text() === 'More tasks')!.trigger('click')
      await flushPromises()
      expect(firstPageReads).toBe(2)
      expect(wrapper.get('[aria-label="Delivery tasks"]').text()).toContain('revised-task')
      expect(wrapper.find('[aria-label="Task pages"]').exists()).toBe(false)
    } finally { wrapper.unmount() }
  })

})
