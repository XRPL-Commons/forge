import { ForgeApiError } from '~/composables/useForgeApi'
import type { FactoryView } from '~/utils/factory'
import {
  accessRows, actionMessage, deploymentCard, displayStatus, hintText,
  protectionReport,
  type FactoryDeployment, type RepositoriesView, type RepositoryRow,
} from '~/utils/repositories'


export function useRepository(repoId: import('vue').Ref<number | null>, active: import('vue').Ref<boolean> = ref(true), deploymentActive: import('vue').Ref<boolean> = ref(true)) {
  const UNAVAILABLE = 'Repository setup is unavailable. Return to Repositories and refresh.'
  const REFRESH_DONE = 'Access refreshed.'
  const REFRESH_UNFINISHED = 'The access check did not finish. Refresh access to try again.'
  const REFRESH_FAILED = 'Could not refresh access. Try again.'
  const REFRESH_DEADLINE = 90000

  const api = useForgeApi()
  // Any POST is in flight (check, enable): polling yields until it lands.
  const busy = ref(false)
  const enabling = ref(false)
  const error = ref('')
  const query = ref('')
  const refreshing = ref(false)
  const accessNote = ref('')
  // One auto-check per observation: `revision:protection_status:observed_at`.
  const autoCheckKey = ref('')
  let autoCheckRetryAt = 0
  const deployError = ref('')
  const environmentError = ref('')
  const mirrorError = ref('')
  const mirrorNotice = ref('')
  const environmentNotice = ref('')
  const environmentAction = ref<'scan' | 'confirm' | 'automatic' | null>(null)
  const rejecting = ref(false)
  const deployments = ref<Record<string, FactoryDeployment>>({})
  let loadingDeployments = false
  let refreshStarted: number | null = null
  let refreshTimer: ReturnType<typeof setTimeout> | undefined

  const poll = useResource<RepositoriesView>(() => '/api/repositories' + (repoId.value === null ? '' : '?repo_id=' + repoId.value), async (path) => {
    const id = repoId.value
    const view = await api.get<RepositoriesView>(path, { timeout: 30000 })
    // A view without the requested repository is not a page: the operator is
    // given an error instead of an empty shell.
    if (!Array.isArray(view.repositories) || (id !== null && !view.repositories.length)) throw new ForgeApiError(200, UNAVAILABLE)
    // What Forge last observed of a tracked deployment lives on the factory
    // view, not on this one. It is read beside the page, never in front of it:
    // the card renders from the profile whether or not it answers.
    if (deploymentActive.value && view.repositories.find(candidate => candidate.id === id)?.deployment?.status === 'confirmed') void loadDeployments()
    return view
  // Repository setup wants a faster tick than the rest of the app (an access
  // check lands within seconds), but never slower than the configured one —
  // which is how the e2e harness's 300ms reaches this page too.
  }, { interval: Math.min(3000, useRuntimeConfig().public.pollInterval), allowed: () => active.value && !busy.value, onRead: observe })

  // The observation carries an age ("deploy succeeded 12 min ago"), which the
  // server cannot know; it appears once mounted and ticks with the clock.
  const now = ref(0)
  let ticker: ReturnType<typeof setInterval> | undefined
  onMounted(() => {
    poll.start()
    if (view.value) maybeAutoCheck(view.value)
    now.value = Math.floor(Date.now() / 1000)
    ticker = setInterval(() => { now.value = Math.floor(Date.now() / 1000) }, 30000)
  })
  onBeforeUnmount(() => { clearTimeout(refreshTimer); clearInterval(ticker) })

  // A different repository is a different page: drop the per-repository state
  // and load again immediately instead of waiting for the next tick.
  watch(repoId, async () => {
    autoCheckKey.value = ''
    autoCheckRetryAt = 0
    error.value = ''
    deployError.value = ''
    environmentError.value = ''
    mirrorError.value = ''
    // A refresh started before the navigation resolves with the previous
    // route's payload, which holds no row
    // for the new repository: ask once more rather than leave the page empty
    // until the next tick.
    await poll.refresh()
    if (!row.value) await poll.refresh()
  })

  const view = computed(() => poll.data.value)
  const live = computed(() => !!view.value?.activation_available)
  // Strictly the requested repository: a payload still describing the previous
  // route (the picker's list, or another repo) renders nothing rather than the
  // wrong repository's setup.
  const row = computed<RepositoryRow | null>(() => {
    if (repoId.value === null) return null
    return (view.value?.repositories ?? []).find(candidate => candidate.id === repoId.value) ?? null
  })
  const report = computed(() => protectionReport(row.value))
  const shown = computed(() => displayStatus(row.value))
  const displayReady = computed(() => shown.value === 'ready')
  const ready = computed(() => row.value?.protection_status === 'ready')

  const pickerRows = computed(() => {
    const needle = query.value.trim().toLowerCase()
    return (view.value?.repositories ?? []).filter(candidate => candidate.name.toLowerCase().includes(needle))
  })
  const connections = computed(() => accessRows(view.value?.connections, row.value))
  const accessDescription = computed(() => row.value
    ? `Give the Forge App access to ${row.value.name}. After making changes on GitHub, refresh access here.`
    : 'Use Manage access to add your repository to a Forge App, then refresh access here.')

  const checking = computed(() => row.value?.protection_status === 'checking')
  const syncing = computed(() => refreshing.value || view.value?.sync.status === 'running')
  const hint = computed(() => row.value ? hintText(row.value) : '')
  const observed = computed(() => repoId.value === null ? null : deployments.value[String(repoId.value)] ?? null)
  // The card exists only once the factory is live: a deployment profile is a
  // decision about a running repository, not a step of guided setup.
  const deployment = computed(() => live.value ? deploymentCard(row.value, observed.value, now.value) : null)

  watch(() => poll.error.value, (failure) => {
    if (failure) error.value = (poll.data.value ? 'Could not refresh. Showing the last received data. ' : '') + failure
    else if (error.value.startsWith('Could not refresh.')) error.value = ''
  })

  function observe(value: RepositoriesView) {
    if (refreshing.value) {
      if (value.sync.status === 'interrupted') finishRefresh(REFRESH_UNFINISHED)
      else if (value.sync.status === 'complete' && value.sync.finished_at !== refreshStarted) finishRefresh(REFRESH_DONE)
    }
    maybeAutoCheck(value)
  }

  // Reaching this page from "Check repository" must not sit on "Checking
  // setup…" until the operator presses refresh: check once per observation.
  // Evaluated on every payload, not only on a changed one, so a check that was
  // refused (its key is reset below) is retried on the next tick.
  function maybeAutoCheck(value: RepositoriesView) {
    const current = repoId.value === null ? null : value.repositories.find(candidate => candidate.id === repoId.value)
    if (import.meta.server || !current || !active.value || Date.now() < autoCheckRetryAt) return
    const key = [value.revision, current.protection_status, current.protection?.observed_at].join(':')
    if (!busy.value && current.access_ready && ['unchecked', 'stale'].includes(current.protection_status) && autoCheckKey.value !== key) {
      autoCheckKey.value = key
      void check()
    }
  }

  watch(() => poll.data.value, (value) => { if (value) maybeAutoCheck(value) })

  function finishRefresh(text: string) {
    refreshing.value = false
    clearTimeout(refreshTimer)
    accessNote.value = text
  }

  async function check() {
    const current = view.value
    if (!current || busy.value || repoId.value === null) return
    busy.value = true
    error.value = ''
    try {
      await api.act('/api/repositories/check', { repo_id: repoId.value, revision: current.revision })
      busy.value = false
      await poll.refresh()
    } catch (failure: unknown) {
      error.value = actionMessage(failure, 'Could not start the check.')
      // Let the next observation try again instead of remembering a refused check.
      autoCheckKey.value = ''
      autoCheckRetryAt = Date.now() + 3000
    } finally {
      busy.value = false
    }
  }

  async function enable() {
    const current = view.value
    if (!current || busy.value || !ready.value || repoId.value === null) return
    busy.value = true
    enabling.value = true
    error.value = ''
    try {
      const next = await api.act<RepositoriesView>('/api/repositories/enable', { repo_id: repoId.value, revision: current.revision })
      // The response is the new snapshot (contracts §2), so the page settles
      // without waiting for the next poll; `replace` also discards the GET that
      // may already be in flight and keeps the poll's signature in step.
      if (Array.isArray(next?.repositories)) poll.replace(next)
      busy.value = false
    } catch (failure: unknown) {
      error.value = actionMessage(failure, 'Could not enable this repository.')
      busy.value = false
      await poll.refresh()
    } finally {
      busy.value = false
      enabling.value = false
    }
  }

  watch(deploymentActive, value => { if (value && row.value?.deployment?.status === 'confirmed') void loadDeployments() })

  async function loadDeployments() {
    if (loadingDeployments) return
    loadingDeployments = true
    try {
      const factory = await api.get<FactoryView>('/api/factory', { timeout: 15000 })
      deployments.value = factory.deployments ?? {}
    } catch {
      // A paused or unreachable factory says nothing about the profile: the
      // card keeps the last observation it had rather than claiming a failure.
    } finally {
      loadingDeployments = false
    }
  }

  // Confirm, reject and re-read are the same POST with the same envelope
  // (contracts §2): the response is the new snapshot, revision included.
  async function decide(action: 'confirm' | 'reject' | 'reread', extra: Record<string, unknown> = {}) {
    const current = view.value
    if (!current || busy.value || repoId.value === null) return
    busy.value = true
    deployError.value = ''
    try {
      const next = await api.act<RepositoriesView>('/api/repositories/deployment', {
        repo_id: repoId.value, revision: current.revision, action, ...extra,
      })
      if (Array.isArray(next?.repositories)) poll.replace(next)
      busy.value = false
    } catch (failure: unknown) {
      deployError.value = actionMessage(failure, 'Could not save this deployment decision.')
      busy.value = false
      await poll.refresh()
    } finally {
      busy.value = false
    }
  }

  async function rejectDeployment(reason: string) {
    rejecting.value = false
    await decide('reject', { reason })
  }

  async function updateEnvironment(action: 'scan' | 'confirm' | 'automatic') {
    const current = row.value
    if (!view.value || !current?.environment || busy.value) return
    busy.value = true
    environmentAction.value = action
    environmentNotice.value = ''
    environmentError.value = ''
    try {
      const next = await api.act<RepositoriesView>('/api/repositories/environment', {
        repo_id: current.id, revision: view.value.revision,
        environment_revision: current.environment.revision, action,
      })
      if (repoId.value === current.id) { poll.replace(next); environmentNotice.value = action === 'scan' ? 'Scan requested.' : 'Saved' }
    } catch (failure: unknown) {
      if (repoId.value === current.id) environmentError.value = actionMessage(failure, 'Could not update database requirements.')
      busy.value = false
      await poll.refresh()
    } finally {
      busy.value = false
      environmentAction.value = null
    }
  }

  async function updateMirrorPolicy(denied: import('~/utils/repositories').MirrorDeniedRepository[]) {
    const current = row.value
    if (!view.value || !current || busy.value) return
    busy.value = true
    mirrorError.value = ''; mirrorNotice.value = ''
    try {
      const next = await api.act<RepositoriesView>('/api/repositories/mirror', {
        repo_id: current.id, revision: view.value.revision, denied_repositories: denied,
      })
      if (repoId.value === current.id) { poll.replace(next); mirrorNotice.value = 'Saved' }
    } catch (failure: unknown) {
      if (repoId.value === current.id) mirrorError.value = actionMessage(failure, 'Could not save Git dependency restrictions.')
      busy.value = false
      await poll.refresh()
    } finally {
      busy.value = false
    }
  }

  async function refreshAccess() {
    const current = view.value
    if (!current || refreshing.value) return
    refreshing.value = true
    refreshStarted = current.sync.status === 'complete' ? current.sync.finished_at : null
    clearTimeout(refreshTimer)
    refreshTimer = setTimeout(() => { if (refreshing.value) finishRefresh(REFRESH_UNFINISHED) }, REFRESH_DEADLINE)
    error.value = ''
    accessNote.value = 'Checking App access…'
    try {
      await api.act(live.value ? '/api/settings/sync' : '/api/onboarding', live.value ? {} : { action: 'refresh' })
      await poll.refresh()
    } catch (failure: unknown) {
      finishRefresh('')
      error.value = actionMessage(failure, REFRESH_FAILED)
    }
  }

  async function savePolicy(kind: 'merge-policy' | 'review-scope', value: string) {
    const current = row.value
    if (!current?.merge_check_revision || busy.value) throw new Error('Refresh the repository before saving.')
    busy.value = true
    try {
      const next = await api.act<RepositoriesView>(`/api/repositories/${kind}`, {
        repo_id: current.id, revision: current.merge_check_revision,
        ...(kind === 'merge-policy' ? { mode: value } : { scope: value }),
      })
      if (repoId.value === current.id) poll.replace(next)
    } catch (failure) {
      busy.value = false
      await poll.refresh()
      throw failure
    } finally { busy.value = false }
  }
  const updateMergePolicy = (mode: string) => savePolicy('merge-policy', mode)
  const updateReviewScope = (scope: string) => savePolicy('review-scope', scope)

  async function disable() {
    if (!view.value || repoId.value === null || busy.value) return
    busy.value = true; error.value = ''
    try {
      const next = await api.act<RepositoriesView>('/api/repositories/disable', { repo_id: repoId.value, revision: view.value.revision })
      poll.replace(next)
    } catch (failure) {
      error.value = actionMessage(failure, 'Could not disable automation.')
      busy.value = false
      await poll.refresh()
    } finally { busy.value = false }
  }

  return { poll, view, live, row, report, shown, displayReady, ready, pickerRows, connections, accessDescription, checking, syncing, hint, deployment, busy, enabling, error, query, accessNote, deployError, environmentError, environmentNotice, environmentAction, rejecting, check, enable, decide, rejectDeployment, updateEnvironment, mirrorError, mirrorNotice, updateMirrorPolicy, updateReviewScope, updateMergePolicy, disable, refreshAccess }
}
