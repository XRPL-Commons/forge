// @vitest-environment nuxt
import { describe, it, expect } from 'vitest'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import JobResourceUsage from '../../../app/components/JobResourceUsage.vue'
import type { ResourceUsage } from '../../../app/utils/factory'

function usage(): ResourceUsage {
  return {
    captured_at: 210,
    wall: { seconds: 55, started_at: 150, finished_at: 205, provenance: 'docker_inspect', unavailable_reason: null },
    sample: { sampled_at: 190, cpu_seconds: 2.5, memory_bytes: 1048576, provenance: 'docker_engine_stats' },
    collection: { checked_at: 209, status: 'unavailable', reason: 'container_not_running' },
  }
}
describe('measured execution resources', () => {
  it('keeps stale sample time distinct from terminal wall duration', async () => {
    const wrapper = await mountSuspended(JobResourceUsage, { props: { usage: usage() } })
    expect(wrapper.text()).toContain('Wall duration 55.0 s')
    expect(wrapper.text()).toContain('CPU 2.5 s cumulative')
    expect(wrapper.text()).toContain('Memory 1.0 MiB including cache')
    expect(wrapper.text()).toContain('sampled values, not final totals')
    expect(wrapper.text()).toContain('showing the retained sample')
    expect(wrapper.get('time').attributes('datetime')).toBe(new Date(190000).toISOString())
    wrapper.unmount()
  })
  it('does not display missing measurements as zero or configured limits', async () => {
    const value = usage()
    value.sample = null
    value.wall = { ...value.wall, seconds: null, unavailable_reason: 'not_finished' }
    const wrapper = await mountSuspended(JobResourceUsage, { props: { usage: value } })
    expect(wrapper.text()).toContain('Wall duration unavailable')
    expect(wrapper.text()).toContain('CPU and memory unavailable')
    expect(wrapper.text()).not.toContain('0.0')
    expect(wrapper.find('time').exists()).toBe(false)
    wrapper.unmount()
  })
  it('preserves genuine zero and independently unavailable counters', async () => {
    const value = usage()
    value.sample = { ...value.sample!, cpu_seconds: 0, memory_bytes: null }
    const wrapper = await mountSuspended(JobResourceUsage, { props: { usage: value } })
    expect(wrapper.text()).toContain('CPU 0.0 s cumulative')
    expect(wrapper.text()).toContain('Memory unavailable')
    wrapper.unmount()
  })
})
