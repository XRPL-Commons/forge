// @vitest-environment nuxt
import { describe, it, expect } from 'vitest'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import IssueWorkerOutput from '../../../app/components/IssueWorkerOutput.vue'

describe('issue worker output', () => {
  it('renders shared live output and shows raw heartbeats only on request', async () => {
    const heartbeat = '{"runner": "alive", "at": 1700000000}'
    const wrapper = await mountSuspended(IssueWorkerOutput, { props: {
      jobs: [], selectedJob: 'job-1', open: true, updatedAt: Date.now(),
      output: { ok: true, live: true, logs: `building\n${heartbeat}`, text: 'building' },
    } })
    expect(wrapper.get('pre').text()).toBe('building')
    expect(wrapper.get('#issue-output-live').text()).toContain('live')
    await wrapper.get('#issue-output-raw').trigger('click')
    expect(wrapper.get('pre').text()).toContain(heartbeat)
    await wrapper.setProps({ selectedJob: 'job-2', output: { ok: true, logs: 'finished', text: 'finished' } })
    expect(wrapper.get('pre').text()).toBe('finished')
    expect(wrapper.find('#issue-output-live').exists()).toBe(false)
    expect(wrapper.find('#issue-output-raw').exists()).toBe(false)
    wrapper.unmount()
  })
})
