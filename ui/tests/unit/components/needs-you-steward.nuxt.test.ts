// @vitest-environment nuxt
import { describe, expect, it } from 'vitest'
import { mountSuspended, mockComponent } from '@nuxt/test-utils/runtime'
import { flushPromises } from '@vue/test-utils'
import { factoryRow } from '../support/rows'
import NeedsYouModal from '../../../app/components/NeedsYouModal.vue'

mockComponent('StewardDecision', { props: ['issueKey'], template: '<div data-loaded-decision>{{ issueKey }}</div>' })

const now = 1_000_000
const view = {
  ok: true as const, controls_token: 'synthetic', active: true, operator: null,
  repositories: [{ id: 1, name: 'example/project', enabled: true }],
  observation: { at: now - 10, errors: [] }, actions: [], jobs: [],
  runtime: { enabled: true, slots: { build: 2, review: 1, audit: 1, recovery: 1 }, cycle_seconds: 30 },
  runtime_revision: 'rev',
  issues: [90, 91].map(number => factoryRow({
    key: `1:${number}`, repo_id: 1, repo: 'example/project', number, title: `Issue ${number}`,
    state: 'hold', reason: 'Repeated failure requires a supported change of approach',
    active: null, failure_count: 3, history: [], attention_kind: 'approach',
    steward_decision: { decision_id: `decision-${number}`, digest: 'a'.repeat(64), status: 'proposed' },
  })),
}

describe('attention choices', () => {
  it('mounts only the expanded decision and removes it when the modal closes', async () => {
    const wrapper = await mountSuspended(NeedsYouModal, {
      props: { view, now, open: true },
      global: { stubs: {
        UModal: { template: '<section><slot name="body" /></section>' },
      } },
    })
    expect(wrapper.findAll('[data-loaded-decision]')).toHaveLength(0)
    const buttons = () => wrapper.findAll('button').filter(button => /steward choices|Hide choices/.test(button.text()))
    expect(buttons()).toHaveLength(2)
    await buttons()[0]!.trigger('click'); await flushPromises()
    expect(wrapper.findAll('[data-loaded-decision]')).toHaveLength(1)
    expect(wrapper.get('[data-loaded-decision]').text()).toBe('1:90')
    await buttons()[1]!.trigger('click'); await flushPromises()
    expect(wrapper.findAll('[data-loaded-decision]')).toHaveLength(1)
    expect(wrapper.get('[data-loaded-decision]').text()).toBe('1:91')
    await wrapper.setProps({ open: false })
    expect(wrapper.findAll('[data-loaded-decision]')).toHaveLength(0)
    await wrapper.setProps({ open: true })
    expect(wrapper.findAll('[data-loaded-decision]')).toHaveLength(0)
    wrapper.unmount()
  })
})
