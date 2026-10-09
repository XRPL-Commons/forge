// @vitest-environment nuxt
import { describe, it, expect } from 'vitest'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import IssueReviewFindings from '../../../app/components/IssueReviewFindings.vue'
import type { IssueDetail } from '../../../app/utils/issues'

describe('issue review findings', () => {
  it('keeps review text collapsed until the operator opens and expands it', async () => {
    const detail = { key: '1:601', reviews: [{ role: 'review', outcome: 'changes_requested', summary: 'Check the edge case.\n' + 'Detailed evidence. '.repeat(30) }] } as unknown as IssueDetail
    const wrapper = await mountSuspended(IssueReviewFindings, { props: { detail } })
    expect(wrapper.find('#issue-reviews').exists()).toBe(false)
    await wrapper.findAll('button').find(button => button.text().includes('Review findings'))!.trigger('click')
    const more = wrapper.findAll('button').find(button => button.text() === 'More')!
    expect(more.exists()).toBe(true)
    await more.trigger('click')
    expect(wrapper.get('#issue-reviews').text()).toContain('Detailed evidence.')
    expect(wrapper.findAll('button').some(button => button.text() === 'Less')).toBe(true)
    wrapper.unmount()
  })
})
