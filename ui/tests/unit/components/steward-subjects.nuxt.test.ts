// @vitest-environment nuxt
import { describe, it, expect } from 'vitest'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import RankedChoices from '../../../app/components/RankedChoices.vue'
import type { RankedDecision } from '../../../app/utils/steward'

describe('shared ranked choices for conversation turns', () => {
  it('renders answer-only as non-mutating and sends only the selected alternative identity', async () => {
    const decision: RankedDecision = { version: 1, decision_id: 'reply', digest: 'a'.repeat(64), confidence: .9,
      recommended_choice_id: 'answer', brief: null, choices: [
        { id: 'answer', action: 'answer', parameters: {}, reason: 'The saved evidence answers the question.', judgement: 'obvious' },
        { id: 'pause', action: 'pause', parameters: {}, reason: 'Pause if the requirements need changing.', judgement: 'judgement' },
      ] }
    const wrapper = await mountSuspended(RankedChoices, { props: { decision } })
    expect(wrapper.get('[data-choice-id="answer"] button').attributes('disabled')).toBeDefined()
    await wrapper.get('[data-choice-id="pause"] button').trigger('click')
    expect(wrapper.emitted('select')).toEqual([[{ decision_id: 'reply', choice_id: 'pause', digest: 'a'.repeat(64) }]])
    wrapper.unmount()
  })
})
