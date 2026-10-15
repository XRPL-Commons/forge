import type { Ref } from 'vue'
import type { RankedDecision, StewardSelection } from '~/utils/steward'
interface PlanningMember { issue_id: string; github_number: number; github_state: 'open' | 'closed'; observed_at: number; title: string; candidate_id: string | null; pull_number: number | null; head_oid: string | null; candidate_author: { id: number; type: string; login: string } | null }
export interface PlanningRow { steward_decision?: RankedDecision | null; selected_choice_id?: string | null; id: string; revision: number; action: string; reason: string; rule_quote: string | null; parameters: Record<string, any>; status: 'proposed' | 'accepted' | 'rejected'; members: PlanningMember[]; members_next: string | null; member_count: number; effects: { id: string; status: 'pending' | 'running' | 'uncertain' | 'confirmed' | 'failed' | 'superseded' }[] }
export interface Proposal { id: string; version: number; status: string; rationale: string; rows: PlanningRow[]; next: number | null; total: number; run: { status: string; error: string | null } | null }
export interface RepositoryNote { note_id: string; revision: number; kind: string; content: string; actor_id: string; sources: { url: string; kind: string; identity: string; description: string }[]; needs_refresh: boolean }
interface PlanningView { repository: { id: number; name: string }; planning: { revision: number; rules: string; proposals_enabled: boolean; runs: { id: string; status: string; error: string | null }[]; notes: RepositoryNote[] }; proposals: { items: { id: string; status: string; created_at: number }[]; next: string | null } }
export function useRepositoryPlanning(repositoryId: Ref<number>) {
  const api = useForgeApi()
  const path = computed(() => `/api/repositories/planning?repository_id=${repositoryId.value}`)
  const resource = useResource<PlanningView>(path, p => api.get(p), { interval: 30000, maxAge: 30000 })
  const proposal = ref<Proposal | null>(null), busy = ref(false), error = ref('')
  const selected = ref<string | null>(null)
  async function open(id: string, after?: number) {
    const page = await api.get<{ proposal: Proposal }>(`${path.value}&proposal_id=${encodeURIComponent(id)}${after === undefined ? '' : `&after=${after}`}`)
    proposal.value = after === undefined ? page.proposal : { ...page.proposal, rows: [...(proposal.value?.rows ?? []), ...page.proposal.rows] }
    selected.value = id
  }
  async function history() {
    const current = resource.data.value
    if (!current?.proposals.next) return
    const page = await api.get<PlanningView>(`${path.value}&after=${encodeURIComponent(current.proposals.next)}`)
    resource.replace({ ...page, proposals: { ...page.proposals, items: [...current.proposals.items, ...page.proposals.items] } })
  }
  async function members(row: PlanningRow) {
    if (!row.members_next) return
    const page = await api.get<{ members: { items: PlanningMember[]; next: string | null } }>(`${path.value}&row_id=${row.id}&after=${encodeURIComponent(row.members_next)}`)
    row.members.push(...page.members.items); row.members_next = page.members.next
  }
  async function act(operation: Record<string, unknown>) {
    if (busy.value) return
    busy.value = true; error.value = ''
    try {
      const receipt = await api.act<{ proposal_id?: string }>('/api/repositories/planning', { repository_id: repositoryId.value, operation: { request_id: crypto.randomUUID(), ...operation } })
      await resource.refresh()
      if (receipt.proposal_id) await open(receipt.proposal_id)
      return receipt
    } catch (e) { error.value = e instanceof Error ? e.message : 'Refresh the proposal before trying again.' }
    finally { busy.value = false }
  }
  async function decide(accepted: string[], rejected: string[]) {
    if (!proposal.value) return
    await act({ action: 'accept', proposal_id: proposal.value.id, expected_version: proposal.value.version, accept_rows: accepted, reject_rows: rejected })
  }
  async function choose(row: PlanningRow, selection: StewardSelection) {
    if (!proposal.value || selection.decision_id !== row.id) return
    await act({ action: 'choose', proposal_id: proposal.value.id, row_id: row.id, expected_version: proposal.value.version,
      choice_id: selection.choice_id, digest: selection.digest })
  }
  onMounted(resource.start)
  return { resource, view: resource.data, proposal, selected, busy, error, open, members, history, act, decide, choose }
}
