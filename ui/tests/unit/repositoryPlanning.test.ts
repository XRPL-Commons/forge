import { describe, it, expect, vi, beforeEach } from 'vitest'
import { ref } from 'vue'
const { get, act, entry } = vi.hoisted(() => ({ get: vi.fn(), act: vi.fn(), entry: { value: null as any } }))
vi.mock('../../app/composables/useForgeApi', () => ({ useForgeApi: () => ({ get, act }) }))
vi.mock('../../app/composables/useResource', async () => {
  const { ref } = await import('vue')
  return { useResource: () => ({ data: entry, error: ref(null), start: vi.fn(), refresh: vi.fn(), replace: (value: unknown) => { entry.value = value } }) }
})
const { useRepositoryPlanning } = await import('../../app/composables/useRepositoryPlanning')
beforeEach(() => { get.mockReset(); act.mockReset(); entry.value = null })
const proposal = (version = 2, rows: any[] = [], next: number | null = null) => ({ id: 'proposal', version, status: 'proposed', rationale: 'Related work', rows, next, total: 2, run: null })
describe('repository proposal controls', () => {
  it('loads proposals and member lists only when requested and appends keyset pages', async () => {
    const planning = useRepositoryPlanning(ref(1))
    expect(get).not.toHaveBeenCalled()
    get.mockResolvedValueOnce({ proposal: proposal(2, [{ id: 'first' }], 0) })
    await planning.open('proposal')
    get.mockResolvedValueOnce({ proposal: proposal(2, [{ id: 'second' }]) })
    await planning.open('proposal', 0)
    expect(planning.proposal.value?.rows.map(row => row.id)).toEqual(['first', 'second'])
    expect(get.mock.calls[1]![0]).toContain('&after=0')
  })
  it('sends the displayed proposal version and distinct explicit accept/reject choices', async () => {
    const planning = useRepositoryPlanning(ref(1))
    planning.proposal.value = proposal(7)
    act.mockResolvedValue({ proposal_id: 'proposal' }); get.mockResolvedValue({ proposal: proposal(8) })
    await planning.decide(['first'], ['second'])
    expect(act).toHaveBeenCalledWith('/api/repositories/planning', { repository_id: 1, operation: expect.objectContaining({
      action: 'accept', proposal_id: 'proposal', expected_version: 7, accept_rows: ['first'], reject_rows: ['second'], request_id: expect.any(String),
    }) })
    expect(planning.proposal.value?.version).toBe(8)
  })
  it('preserves the reviewable proposal and reports a stale decision without silently retrying', async () => {
    const planning = useRepositoryPlanning(ref(1))
    planning.proposal.value = proposal(7)
    act.mockRejectedValue(new Error('Issue changed. Refresh the proposal.'))
    await planning.decide(['first'], [])
    expect(planning.error.value).toContain('Issue changed')
    expect(planning.proposal.value?.version).toBe(7)
    expect(act).toHaveBeenCalledTimes(1); expect(get).not.toHaveBeenCalled()
  })
})
