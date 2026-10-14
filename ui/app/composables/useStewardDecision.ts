import { computed, onMounted, watch, type MaybeRefOrGetter, toValue } from 'vue'
import { ForgeApiError } from './useForgeApi'
import type { StewardDecisionMarker, StewardDecisionResponse, StewardSelection } from '~/utils/steward'

type ChoiceOperation = StewardSelection & { request_id: string; state: 'submitting' | 'uncertain' | 'selected' }
const identity = (value: Pick<StewardSelection, 'decision_id' | 'digest'>) => `${value.decision_id}:${value.digest}`

export function useStewardDecision(issueKey: MaybeRefOrGetter<string>, options: {
  allowed?: () => boolean
  expected?: () => StewardDecisionMarker | null | undefined
} = {}) {
  const api = useForgeApi()
  const resources = useResources()
  const operations = useState<Record<string, ChoiceOperation>>('steward-choice-operations', () => ({}))
  const errors = useState<Record<string, string>>('steward-choice-errors', () => ({}))
  const mounted = ref(false)
  const resource = useResource<StewardDecisionResponse>(
    () => `/api/issues/steward?key=${encodeURIComponent(toValue(issueKey))}`,
    path => api.get<StewardDecisionResponse>(path),
    { server: false, allowed: () => mounted.value && (options.allowed?.() ?? true), onRead: reconcile },
  )
  const decision = computed(() => resource.data.value?.decision ?? null)
  const operationKey = computed(() => decision.value ? identity(decision.value) : '')
  const operation = computed(() => operations.value[operationKey.value] ?? null)
  const pending = computed(() => operation.value?.state === 'submitting')
  const stale = computed(() => {
    const expected = options.expected?.()
    const current = decision.value
    return resource.stale.value || current?.status === 'stale'
      || !!(expected && current && (identity(expected) !== identity(current)
        || expected.status !== 'proposed' && current.status === 'proposed'))
  })
  const disabled = computed(() => !mounted.value || !(options.allowed?.() ?? true) || stale.value
    || !decision.value || decision.value.status !== 'proposed' || !!decision.value.application || !!operation.value)
  const message = computed(() => {
    const current = decision.value
    if (current?.status === 'applied' || current?.application?.status === 'applied') return 'This choice has been applied.'
    if (current?.application?.status === 'failed') return current.application.message || 'Forge could not apply this choice. Refresh to check the next step.'
    if (current?.status === 'selected' || current?.application?.status === 'pending' || operation.value?.state === 'selected')
      return 'Your choice is recorded. Forge is preparing the next step.'
    if (pending.value) return 'Recording your choice…'
    if (operation.value?.state === 'uncertain') return 'The reply was interrupted. Refresh to check whether Forge recorded your choice.'
    if (stale.value) return 'These choices have changed or could not be refreshed. Refresh before choosing.'
    return errors.value[operationKey.value] ?? null
  })

  // A lost reply must not turn a remount or page reload into a second write.
  // Store only the submitted identifiers, never credentials or decision prose.
  function remember(value: ChoiceOperation) {
    sessionStorage.setItem(`forge-steward:${identity(value)}`, JSON.stringify(value))
    operations.value[identity(value)] = value
  }
  function reconcile(response: StewardDecisionResponse) {
    const current = response.decision
    if (!import.meta.client || !current) return
    const key = identity(current)
    if (!operations.value[key]) {
      try {
        const raw = sessionStorage.getItem(`forge-steward:${key}`)
        const saved = raw ? JSON.parse(raw) : null
        if (saved?.decision_id === current.decision_id && saved?.digest === current.digest
            && typeof saved.choice_id === 'string' && typeof saved.request_id === 'string') {
          operations.value[key] = { decision_id: saved.decision_id, digest: saved.digest,
            choice_id: saved.choice_id, request_id: saved.request_id, state: 'uncertain' }
        }
      } catch { /* Current server state remains authoritative; no stored action is replayed. */ }
    }
    if (current.status !== 'proposed' || current.application) {
      delete operations.value[key]
      try { sessionStorage.removeItem(`forge-steward:${key}`) } catch { /* Keeping a lock cannot replay an action. */ }
    }
  }
  async function select(selection: StewardSelection) {
    const current = decision.value
    if (disabled.value || !current || identity(current) !== identity(selection)
        || !current.choices.some(choice => choice.id === selection.choice_id)) return false
    const key = identity(selection)
    const operation: ChoiceOperation = { ...selection, request_id: crypto.randomUUID(), state: 'submitting' }
    delete errors.value[key]
    try { remember(operation) }
    catch {
      errors.value[key] = 'Your browser could not preserve this request. Allow session storage before choosing.'
      return false
    }
    try {
      const response = await api.act<StewardDecisionResponse>('/api/issues/steward', {
        key: toValue(issueKey), ...selection, request_id: operation.request_id,
      })
      operations.value[key] = { ...operation, state: 'selected' }
      // The response belongs to the issue that was submitted, even if the
      // operator has navigated elsewhere while the request was in flight.
      if (operationKey.value === key) {
        resource.replace(response)
        reconcile(response)
      }
      return true
    } catch (error) {
      const rejected = error instanceof ForgeApiError && error.status >= 400 && error.status < 500 && error.status !== 408
      if (rejected) {
        delete operations.value[key]
        try { sessionStorage.removeItem(`forge-steward:${key}`) } catch { /* The next read can still resolve the lock. */ }
        errors.value[key] = error.message
        // A refused choice must be read again; the API invalidates cached data.
        resources.invalidate(path => path.startsWith('/api/issues/steward?'))
      } else {
        operations.value[key] = { ...operation, state: 'uncertain' }
      }
      return false
    }
  }
  watch(() => {
    const expected = options.expected?.()
    return expected ? `${identity(expected)}:${expected.status}` : null
  }, () => {
    if (mounted.value && (options.allowed?.() ?? true)) void resource.refresh()
  })
  onMounted(() => { mounted.value = true; resource.start() })
  return { decision, resource, disabled, pending, operation, message, select, refresh: () => resource.refresh() }
}
