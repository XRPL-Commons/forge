// @vitest-environment nuxt
import { describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'
import { mountSuspended, mockComponent, mockNuxtImport } from '@nuxt/test-utils/runtime'
import IssueNextStep from '../../../app/components/IssueNextStep.vue'

const { context } = vi.hoisted(() => ({ context: vi.fn() }))
mockNuxtImport('useIssueDetailContext', () => context)
mockComponent('StewardDecision', { template: '<div data-steward>Steward choices</div>' })
mockComponent('IssueWorkbench', { template: '<section><slot /></section>' })

describe('current worker claims', () => {
  it('shows both claims and their answer action beside a Steward choice', async () => {
    context.mockReturnValue({
      detail: ref({ key: '1:2', dependencies: [], status: 'attention', actions: ['retry'],
        steward_decision: { decision_id: 'decision', digest: 'a'.repeat(64), status: 'proposed' },
        decision_request: { question: 'Which interface?', options: ['Stable', 'Experimental'],
          recommendation: 'Stable', evidence: 'The client supports both.\n\nScope needs adjustment: Split the service rewrite.',
          acknowledgment_scope: 'Your answer acknowledges these claims for this issue only.' },
      }), look: ref({}), bubble: ref(null), approvals: ref([]), holds: ref([]), facts: ref(null), live: ref({ state: 'idle' }),
      moreItems: ref([]), actions: ref([]), primary: ref('retry'), secondary: ref(null),
      checked: ref(null), checkRequest: { pending: ref(false) }, technical: ref(null),
      verificationRequest: { pending: ref(false) }, ask: vi.fn(), poll: { refresh: vi.fn() },
      ACTION_LABELS: { retry: 'Retry attempt' },
    })
    const wrapper = await mountSuspended(IssueNextStep)
    expect(wrapper.find('[data-steward]').exists()).toBe(true)
    expect(wrapper.get('#issue-decision').text()).toContain('Which interface?')
    expect(wrapper.get('#issue-decision').text()).toContain('Split the service rewrite.')
    expect(wrapper.get('#issue-decision').text()).toContain('this issue only')
    expect(wrapper.get('#issue-actions').text()).toContain('Retry attempt')
    wrapper.unmount()
  })
})
