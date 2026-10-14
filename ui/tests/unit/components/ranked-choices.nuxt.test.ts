// @vitest-environment nuxt
import { describe, expect, it } from 'vitest'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import RankedChoices from '../../../app/components/RankedChoices.vue'
import type { StewardDecisionContent } from '../../../app/utils/steward'

const decision: StewardDecisionContent & { decision_id: string; digest: string } = {
  version: 1, decision_id: 'decision-one', digest: 'a'.repeat(64), recommended_choice_id: 'wait', confidence: .8,
  brief: { choice_id: 'retry', instructions: 'Read the existing branch before changing it.', evidence_refs: ['attempt-one'] },
  choices: [
    { id: 'retry', action: 'retry', parameters: { timeout_seconds: 1200 }, reason: 'The final checks need more time.', judgement: 'obvious' },
    { id: 'wait', action: 'wait_on', parameters: { repository_id: 1, issue_number: 8 }, reason: 'Issue #8 supplies the required API.', judgement: 'judgement' },
    { id: 'drop', action: 'drop', parameters: { intent_note: 'The intent is already covered by the replacement.' }, reason: 'The scope is now redundant.', judgement: 'judgement' },
  ],
}

describe('shared ranked steward choices', () => {
  it('puts the recommendation first and selects only the exact stored identity', async () => {
    const wrapper = await mountSuspended(RankedChoices, { props: { decision, repositories: [{ id: 1, name: 'example/dependency' }] } })
    expect(wrapper.findAll('[data-choice-id]').map(row => row.attributes('data-choice-id'))).toEqual(['wait', 'retry', 'drop'])
    expect(wrapper.get('[data-choice-id="wait"]').text()).toContain('Recommended')
    expect(wrapper.get('[data-choice-id="wait"]').text()).toContain('example/dependency · #8')
    expect(wrapper.text()).toContain('Steward confidence: 80%')
    expect(wrapper.get('[data-choice-id="retry"]').text()).toContain('20 minutes')
    expect(wrapper.get('[data-choice-id="retry"]').text()).toContain(decision.brief!.instructions)
    expect(wrapper.get('[data-choice-id="wait"]').text()).not.toContain(decision.brief!.instructions)
    expect(wrapper.get('[data-choice-id="drop"]').text()).toContain(decision.choices[2]!.reason)
    await wrapper.get('[data-choice-id="wait"] button').trigger('click')
    expect(wrapper.emitted('select')).toEqual([[{ decision_id: 'decision-one', choice_id: 'wait', digest: 'a'.repeat(64) }]])
    await wrapper.setProps({ selectedChoice: 'wait', disabled: true })
    expect(wrapper.get('[data-choice-id="wait"]').text()).toContain('Chosen')
    expect(wrapper.text()).not.toContain(decision.brief!.instructions)
    wrapper.unmount()
  })

  it('prevents selection while stale or while any choice is being submitted', async () => {
    const wrapper = await mountSuspended(RankedChoices, { props: { decision, disabled: true } })
    for (const button of wrapper.findAll('button')) {
      expect(button.attributes('disabled')).toBeDefined()
      await button.trigger('click')
    }
    expect(wrapper.emitted('select')).toBeUndefined()
    await wrapper.setProps({ disabled: false, pendingChoice: 'retry' })
    expect(wrapper.get('[data-choice-id="retry"] button').text()).toContain('Submitting')
    expect(wrapper.findAll('button').every(button => button.attributes('disabled') !== undefined)).toBe(true)
    wrapper.unmount()
  })

  it('shows the exact split drafts as text before the operator chooses', async () => {
    const split = { ...decision, recommended_choice_id: 'split', brief: null, choices: [{
      id: 'split', action: 'split' as const, judgement: 'judgement' as const, reason: 'Keep the first change small.',
      parameters: { intent_note: 'Preserve the full migration intent.', children: [{ title: 'First step', body: '<script>unsafe()</script>', acceptance_criteria: ['Existing users can sign in.'] }] },
    }] }
    const wrapper = await mountSuspended(RankedChoices, { props: { decision: split } })
    expect(wrapper.text()).toContain('Review 1 proposed issues')
    expect(wrapper.text()).toContain('<script>unsafe()</script>')
    expect(wrapper.find('script').exists()).toBe(false)
    expect(wrapper.get('[aria-label="Acceptance criteria"]').text()).toContain('Existing users can sign in.')
    wrapper.unmount()
  })
})
