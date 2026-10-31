import { installationHeadline, installationPending, type InstallationAction, type InstallationView } from '~/utils/installation'

/** The banner, progress page and repository dialog share one cached read and poll. */
export function useInstallation() {
  const api = useForgeApi()
  const route = useRoute()
  const cached = useResources().entry<InstallationView>('/api/installation')
  const poll = useResource<InstallationView>('/api/installation', path => api.get(path), {
    interval: 3000, maxAge: 3000, server: false,
    allowed: () => route.path === '/installation' || Boolean(cached.error.value) || !cached.updatedAt.value || installationPending(cached.data.value),
  })
  const busy = useState('installation-action-busy', () => false)
  const actionError = useState('installation-action-error', () => '')
  const view = computed(() => poll.data.value)
  const pending = computed(() => installationPending(view.value))
  const headline = computed(() => view.value ? installationHeadline(view.value) : '')
  onMounted(poll.start)
  async function act(action: InstallationAction) {
    if (busy.value || !view.value?.generation) return
    busy.value = true
    actionError.value = ''
    try {
      const next = await api.act<InstallationView>('/api/installation', {
        generation: view.value.generation, action: action.id,
        ...(action.proposal_id !== undefined ? { proposal_id: action.proposal_id } : {}),
      })
      poll.replace(next)
    } catch (error) {
      actionError.value = error instanceof Error ? error.message : 'Could not continue setup.'
      await poll.refresh()
    } finally { busy.value = false }
  }
  return { poll, view, pending, headline, busy, actionError, act }
}
