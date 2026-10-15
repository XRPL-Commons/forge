// @vitest-environment nuxt
import { describe, it, expect, vi } from 'vitest'
import { ref, nextTick } from 'vue'
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
      members: [{ issue_id: '1:1', github_number: 1, github_state: 'open', observed_at: 100, title: 'Work', pull_number: 7, head_oid: 'a'.repeat(40) }],
      parameters: { existing_prs: [], dependencies: [], publication_target: { kind: 'existing', pull_number: 7, author: { id: 8, login: 'maintainer', type: 'User' }, ownership_transfer: true } },
    }] }), busy: ref(false), error: ref(''), open: vi.fn(), members: vi.fn(), history: vi.fn(), act: vi.fn(), decide: vi.fn(), choose: vi.fn(),
  }
}
describe('repository planning approval', () => {
  it('attributes planner prose while showing observed state and unapproved actions as facts', async () => {
    setup()
    const claim = 'This pass closed #1.'
    state.current.proposal.value.rationale = claim
    Object.assign(state.current.proposal.value.rows[0], { action: 'close', reason: claim, parameters: { evidence: [] } })
    state.current.view.value.planning.notes = [
      { note_id: 'planner-note', revision: 1, kind: 'decision', content: claim, actor_id: 'supervisor:triage', sources: [], needs_refresh: false },
      { note_id: 'operator-note', revision: 2, kind: 'decision', content: 'Closing is proposed.', actor_id: 'github:7', sources: [], needs_refresh: false },
    ]
    const wrapper = await mountSuspended(PlanningPage)
    expect(wrapper.get('[data-testid="planner-rationale"]').text()).toBe(claim)
    expect(wrapper.text()).toContain('Planner rationale')
    expect(wrapper.text()).toContain('Planner note · decision · revision 1')
    expect(wrapper.text()).toContain('Operator note · decision · revision 2')
    expect(wrapper.get('[data-testid="planning-approval-status"]').text()).toBe('Awaiting approval')
    expect(wrapper.get('[data-testid="planning-issue-state"]').text()).toContain('Last observed open')
    expect(wrapper.get('[data-testid="planning-issue-state"] time').attributes('datetime')).toBe('1970-01-01T00:01:40.000Z')
    expect(wrapper.find('[data-testid="planning-effect-status"]').exists()).toBe(false)
    expect(state.current.decide).not.toHaveBeenCalled()
    wrapper.unmount()
  })
  it('keeps approval separate from pending or confirmed GitHub effects', async () => {
    setup()
    const row = state.current.proposal.value.rows[0]
    Object.assign(row, { action: 'close', status: 'accepted', parameters: { evidence: [] }, effects: [{ id: 'effect', status: 'pending' }] })
    const wrapper = await mountSuspended(PlanningPage)
    expect(wrapper.get('[data-testid="planning-approval-status"]').text()).toBe('Approved')
    expect(wrapper.get('[data-testid="planning-effect-status"]').text()).toBe('Awaiting GitHub confirmation')
    expect(wrapper.get('[data-testid="planning-issue-state"]').text()).toContain('Last observed open')
    row.effects[0].status = 'confirmed'
    row.members[0].github_state = 'closed'
    row.members[0].observed_at = 200
    await nextTick()
    expect(wrapper.get('[data-testid="planning-effect-status"]').text()).toBe('Verified on GitHub')
    expect(wrapper.get('[data-testid="planning-issue-state"]').text()).toContain('Last observed closed')
    expect(wrapper.get('[data-testid="planning-issue-state"] time').attributes('datetime')).toBe('1970-01-01T00:03:20.000Z')
    expect(state.current.decide).not.toHaveBeenCalled()
    wrapper.unmount()
  })
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
  it('lets an operator change a single issue disposition before accepting it', async () => {
    setup()
    Object.assign(state.current.proposal.value.rows[0], { action: 'question', parameters: { question: 'What should change?' } })
    const wrapper = await mountSuspended(PlanningPage)
    await wrapper.findAll('button').find(button => button.text() === 'Edit')!.trigger('click')
    await wrapper.get('select').setValue('park')
    await wrapper.get('form input').setValue('content')
    await wrapper.get('form').trigger('submit')
    expect(state.current.act).toHaveBeenCalledWith(expect.objectContaining({ action: 'edit', expected_version: 2,
      value: expect.objectContaining({ action: 'park', members: ['1:1'], parameters: { add: ['content'] } }),
    }))
    expect(state.current.decide).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('selects a stored ranked alternative before the separate approval', async () => {
    setup()
    const row = state.current.proposal.value.rows[0]
    row.steward_decision = { version: 1, decision_id: row.id, digest: 'a'.repeat(64), confidence: .8,
      recommended_choice_id: 'keep', brief: null, choices: [
        { id: 'keep', action: 'keep', reason: 'Keep useful work.', judgement: 'obvious', parameters: { members: ['1:1'], rule_quote: null, details: {} } },
        { id: 'park', action: 'park', reason: 'Wait for operator context.', judgement: 'judgement', parameters: { members: ['1:1'], rule_quote: null, details: { add: ['hold'] } } },
      ] }
    row.selected_choice_id = 'keep'
    const wrapper = await mountSuspended(PlanningPage)
    await wrapper.get('[data-choice-id="park"] button').trigger('click')
    expect(state.current.choose).toHaveBeenCalledWith(row, { decision_id: row.id, choice_id: 'park', digest: 'a'.repeat(64) })
    expect(state.current.decide).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('Accept it below to approve the change.')
    wrapper.unmount()
  })

})
