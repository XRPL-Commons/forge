import type { IssueDetail } from '~/utils/issues'

interface DeliveryPreview {
  revision: string
  delivery: null | {
    unit_id: string
    member_count: number
    unit_paused: boolean
    pending_decision: boolean
    members: { member_id: string; issue_id: string; number: number }[]
    dependent_members: string[]
    decision: { member_id: string; membership_generation: number; dependent_decisions: Record<string, 'wait_external'> }
  }
}
export type DeliveryAction = 'drop' | 'close_member' | 'dissolve' | 'resume_delivery'

export function useDeliveryDecision(issue: () => IssueDetail) {
  const api = useForgeApi()
  const cache = useState<Record<string, { revision: string; preview: DeliveryPreview }>>('delivery-previews', () => ({}))
  const preview = shallowRef<DeliveryPreview | null>(null)
  const request = useSubmission()
  const decisionId = ref('')
  async function load() {
    const current = issue()
    const cached = cache.value[current.key]
    if (cached?.revision === current.revision) { preview.value = cached.preview; return }
    await request.run(async () => {
      const result = await api.get<DeliveryPreview>(`/api/issues/delivery?key=${encodeURIComponent(current.key)}`)
      cache.value[current.key] = { revision: current.revision, preview: result }
      preview.value = result
    })
  }
  function begin() { decisionId.value = crypto.randomUUID(); request.reset() }
  async function apply(action: DeliveryAction, reason: string) {
    const current = preview.value
    if (!current?.delivery) return false
    const applied = await request.run(() => api.act('/api/issues/delivery', {
      key: issue().key, action, reason, revision: current.revision,
      membership: current.delivery!.decision, request_id: decisionId.value,
    }))
    if (applied) {
      delete cache.value[issue().key]
      current.delivery.pending_decision = true
    }
    return applied
  }
  return { preview, request, load, begin, apply }
}
