// @vitest-environment nuxt
import { describe, it, expect, vi } from 'vitest'
import { ref } from 'vue'
import { mountSuspended, mockNuxtImport } from '@nuxt/test-utils/runtime'
import PlanningPage from '../../../app/pages/planning/[id].vue'
const state = vi.hoisted(() => ({ current: null as any }))
vi.mock('../../../app/composables/useRepositoryPlanning', () => ({ useRepositoryPlanning: () => state.current }))
mockNuxtImport('useRoute', () => () => ({ params: { id: '1' } }))
function setup() {
  state.current = {
    resource: { error: ref(null) }, view: ref({ repository: { id: 1, name: 'example/project' }, planning: { rules: 'Keep related work together', notes: [], proposals_enabled: true, runs: [] }, proposals: { items: [], next: null } }),
    proposal: ref({ id: 'proposal', version: 2, status: 'proposed', rationale: 'One delivery', next: null, total: 1, run: null, rows: [{
      id: 'row', action: 'group', status: 'proposed', reason: 'Related scope', rule_quote: 'Keep related work together', member_count: 1, members_next: null, effects: [],
      members: [{ issue_id: '1:1', github_number: 1, title: 'Work', pull_number: 7, head_oid: 'a'.repeat(40) }],
      parameters: { existing_prs: [], dependencies: [], publication_target: { kind: 'existing', pull_number: 7, author: { id: 8, login: 'maintainer', type: 'User' }, ownership_transfer: true } },
    }] }), busy: ref(false), error: ref(''), open: vi.fn(), members: vi.fn(), history: vi.fn(), act: vi.fn(), decide: vi.fn(),
  }
}
describe('repository planning approval', () => {
  it('shows existing PR transfer and rule evidence before explicit acceptance', async () => {
    setup()
    const wrapper = await mountSuspended(PlanningPage)
    expect(wrapper.text()).toContain('transferring implementation of this PR from maintainer')
    expect(wrapper.text()).toContain('Rule: “Keep related work together”')
    expect(state.current.decide).not.toHaveBeenCalled()
    await wrapper.findAll('button').find(button => button.text() === 'Accept')!.trigger('click')
    expect(state.current.decide).toHaveBeenCalledWith(['row'], [])
    wrapper.unmount()
  })
  it('requires loading all decisions before Accept all and keeps row rejection explicit', async () => {
    setup(); state.current.proposal.value.next = 0
    const wrapper = await mountSuspended(PlanningPage)
    const accept = wrapper.findAll('button').find(button => button.text().startsWith('Accept all'))!
    expect(accept.attributes('disabled')).toBeDefined()
    await wrapper.findAll('button').find(button => button.text() === 'Reject')!.trigger('click')
    expect(state.current.decide).toHaveBeenCalledWith([], ['row'])
    wrapper.unmount()
  })
})
