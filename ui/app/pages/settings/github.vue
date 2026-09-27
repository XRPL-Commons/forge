<script setup lang="ts">
import {
  actionMessage, connectionCards, lastRefreshText, repositoryRows,
  type OnboardingSummary, type SettingsView,
} from '~/utils/repositories'

// Settings · GitHub access (contracts §3.5): the four Forge Apps, their
// verified access, and the repositories they can reach. Capacity and reset
// live on Settings · General, so nothing here is editable and the page has no
// dirty state to guard.
useHead({ title: 'GitHub access' })

const REFRESH_DONE = 'Access refreshed.'
const REFRESH_UNFINISHED = 'The access check did not finish. Refresh access to try again.'
const REFRESH_FAILED = 'Could not refresh access. Try again.'
const REFRESH_DEADLINE = 90000

interface Snapshot { settings: SettingsView; onboarding: OnboardingSummary }

const api = useForgeApi()

// A POST is in flight: polling yields so the table is not re-rendered under
// the operator's hands. Access refresh deliberately keeps polling, because
// only the next poll can observe that the background sync finished.
const busy = ref(false)
const notice = ref('')
const noticeError = ref(false)
const refreshing = ref(false)
const refreshNote = ref('')
const query = ref('')
const checkingRepositories = ref(false)
let refreshStarted: number | null = null
let refreshTimer: ReturnType<typeof setTimeout> | undefined
let noticeTimer: ReturnType<typeof setTimeout> | undefined

const poll = usePoll<Snapshot>(async () => {
  const [settings, onboarding] = await Promise.all([
    api.get<SettingsView>('/api/settings', { timeout: 30000 }),
    api.get<OnboardingSummary>('/api/onboarding', { timeout: 30000 }),
  ])
  return { settings, onboarding }
}, {
  interval: useRuntimeConfig().public.pollInterval,
  allowed: () => !busy.value,
  onChange: observeRefresh,
})

onMounted(() => poll.start())
onBeforeUnmount(() => { clearTimeout(refreshTimer); clearTimeout(noticeTimer) })

const settings = computed(() => poll.data.value?.settings ?? null)
const onboarding = computed(() => poll.data.value?.onboarding ?? null)
const settingUp = computed(() => !!onboarding.value && onboarding.value.phase !== 'active')

const syncStatus = computed(() => settings.value ? lastRefreshText(settings.value) : 'Loading settings…')
const cards = computed(() => settings.value ? connectionCards(settings.value, onboarding.value) : [])
const rows = computed(() => repositoryRows(settings.value?.repositories ?? [], settingUp.value))
const visibleRows = computed(() => {
  const needle = query.value.trim().toLowerCase()
  return needle ? rows.value.filter(row => row.name.toLowerCase().includes(needle)) : rows.value
})
const enabledCount = computed(() => (settings.value?.repositories ?? []).filter(row => row.enabled).length)
const repositoryCount = computed(() => settings.value
  ? `${settingUp.value ? 'Setup in progress' : enabledCount.value + ' enabled'} · ${settings.value.repositories.length} discovered or configured`
  : '')
const syncing = computed(() => refreshing.value || settings.value?.sync.status === 'running')
const repositoriesChecking = computed(() => checkingRepositories.value
  || (settings.value?.repositories ?? []).some(row => row.protection_status === 'checking'))

// The poll answers with the whole page, so its failure is the page's notice.
watch(() => poll.error.value, (failure) => {
  if (failure) {
    notice.value = (poll.data.value ? 'Could not refresh. Showing the last received data. ' : '') + failure
    noticeError.value = true
  } else if (noticeError.value && notice.value.startsWith('Could not refresh.')) {
    notice.value = ''
    noticeError.value = false
  }
})

function say(text: string, error = false) {
  clearTimeout(noticeTimer)
  notice.value = text
  noticeError.value = error
  // A confirmation is read once; an error stays until it is resolved (§3.4).
  if (text && !error) noticeTimer = setTimeout(() => { notice.value = '' }, 6000)
}

// A background sync reports through `sync`, never through its own response:
// it is finished when a fresh `finished_at` arrives, and given up on when the
// supervisor reports `interrupted` or 90 s pass with no verdict.
function observeRefresh(value: Snapshot) {
  if (!refreshing.value) return
  const sync = value.settings.sync
  if (sync.status === 'interrupted') return finishRefresh(REFRESH_UNFINISHED)
  if (sync.status === 'complete' && sync.finished_at !== refreshStarted) finishRefresh(REFRESH_DONE)
}

function finishRefresh(text: string) {
  refreshing.value = false
  clearTimeout(refreshTimer)
  refreshNote.value = text
}

async function refreshAccess() {
  const current = settings.value
  if (!current || refreshing.value) return
  refreshing.value = true
  refreshStarted = current.sync.status === 'complete' ? current.sync.finished_at : null
  clearTimeout(refreshTimer)
  refreshTimer = setTimeout(() => { if (refreshing.value) finishRefresh(REFRESH_UNFINISHED) }, REFRESH_DEADLINE)
  say('')
  refreshNote.value = 'Checking App access…'
  const setup = onboarding.value?.phase === 'setup'
  try {
    await api.act(setup ? '/api/onboarding' : '/api/settings/sync', setup ? { action: 'refresh' } : {})
    await poll.refresh()
  } catch (error: unknown) {
    // A refused refresh is a failure, not a progress note: it belongs in the
    // page's error notice, the same way repository setup reports it.
    finishRefresh('')
    say(actionMessage(error, REFRESH_FAILED), true)
  }
}

async function refreshRepositories() {
  const current = settings.value
  if (!current || busy.value) return
  busy.value = true
  checkingRepositories.value = true
  say('')
  try {
    await api.act('/api/repositories/check', { repo_id: null, revision: current.revision })
    busy.value = false
    await poll.refresh()
  } catch (error: unknown) {
    say(actionMessage(error, 'Could not start the check.'), true)
  } finally {
    busy.value = false
    checkingRepositories.value = false
  }
}

const disableTarget = ref<RepositoryRowModel | null>(null)

function onToggle(row: RepositoryRowModel) {
  if (row.action === 'continue') return navigateTo('/onboarding')
  if (row.action === 'setup') return navigateTo(row.to)
  disableTarget.value = row
}

async function confirmDisable() {
  const target = disableTarget.value
  disableTarget.value = null
  if (target) await disable(target.id)
}

async function disable(id: number) {
  const current = settings.value
  if (!current || busy.value) return
  const repository = current.repositories.find(row => row.id === id)
  if (!repository) return
  busy.value = true
  say('')
  try {
    const config = structuredClone(toRaw(current.config))
    const entry = config.repositories.find(row => row.id === id)
    if (entry) entry.enabled = false
    else config.repositories.push({ id, name: repository.name, enabled: false })
    await api.act('/api/settings', { config, revision: current.revision })
    say('Settings saved.')
    busy.value = false
    await poll.refresh()
  } catch (error: unknown) {
    say(actionMessage(error, 'Settings unavailable'), true)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div>
    <div>
      <div class="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p class="text-xs uppercase tracking-wide text-muted">Settings</p>
          <h1 class="text-3xl font-semibold text-white">GitHub access</h1>
        </div>
        <NuxtLink v-if="settingUp" to="/onboarding" class="text-sm text-sky-200 hover:underline">Continue setup</NuxtLink>
      </div>
      <p class="mt-1 text-sm text-muted">See the four Forge Apps, their verified permissions, and repository access. Refresh after changing access on GitHub.</p>
    </div>

    <p
      v-if="notice"
      id="notice"
      :role="noticeError ? 'alert' : 'status'"
      class="mt-4 rounded-xl border px-4 py-3 text-sm"
      :class="noticeError ? 'border-rose-200/40 text-rose-200' : 'border-line bg-panel text-white'"
    >{{ notice }}</p>

    <section aria-labelledby="github-title" class="mt-5 rounded-xl border border-line bg-bg p-4">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <h2 id="github-title" class="text-sm font-semibold text-white">GitHub connections</h2>
        <UButton
          color="neutral"
          variant="ghost"
          aria-label="Refresh GitHub access"
          title="Refresh GitHub access"
          :disabled="!settings"
          :aria-busy="syncing ? 'true' : undefined"
          @click="refreshAccess"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" class="h-5 w-5" :class="syncing ? 'animate-spin' : ''" aria-hidden="true" focusable="false">
            <path d="M20 7v5h-5M4 17v-5h5" /><path d="M6.1 6.1A8 8 0 0 1 20 12M4 12a8 8 0 0 0 13.9 5.9" />
          </svg>
        </UButton>
      </div>
      <p id="sync-status" class="mt-1 text-xs text-muted">{{ syncStatus }}</p>
      <p v-if="refreshNote" id="refresh-result" role="status" class="mt-1 text-xs text-sky-200">{{ refreshNote }}</p>

      <aside class="mt-3 rounded-xl border border-sky-200/30 bg-sky-200/5 px-4 py-3 text-xs text-sky-200/90">
        <span class="block text-[11px] font-semibold uppercase tracking-wide text-sky-200">Help · repository access</span>
        To add repositories, use <strong>Manage access</strong> for each App on GitHub, then <strong>Refresh</strong> here. Forge manages the App credentials.
      </aside>

      <div id="connections" class="mt-4 grid gap-3 lg:grid-cols-2">
        <ConnectionCard v-for="card in cards" :key="card.role" :card="card" />
      </div>
    </section>

    <section aria-labelledby="repos-title" class="mt-4 rounded-xl border border-line bg-bg p-4">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <div class="flex flex-wrap items-center gap-3">
          <h2 id="repos-title" class="text-sm font-semibold text-white">Repositories</h2>
          <span id="repo-count" class="text-xs text-muted">{{ repositoryCount }}</span>
        </div>
        <div class="flex items-center gap-2">
          <UButton to="/settings/repository" size="xs" color="neutral" variant="outline">Add repository</UButton>
          <UButton
            v-if="!settingUp"
            color="neutral"
            variant="ghost"
            aria-label="Refresh repositories"
            title="Refresh repositories"
            :disabled="busy || !enabledCount"
            :aria-busy="repositoriesChecking ? 'true' : undefined"
            @click="refreshRepositories"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" class="h-5 w-5" :class="repositoriesChecking ? 'animate-spin' : ''" aria-hidden="true" focusable="false">
              <path d="M20 7v5h-5M4 17v-5h5" /><path d="M6.1 6.1A8 8 0 0 1 20 12M4 12a8 8 0 0 0 13.9 5.9" />
            </svg>
          </UButton>
        </div>
      </div>
      <p class="mt-1 text-xs text-muted">Choose Enable to check a repository and finish its setup. Automation starts only after you confirm.</p>

      <label for="repo-search" class="mt-3 block text-xs text-muted">Find a repository</label>
      <UInput id="repo-search" v-model="query" type="search" placeholder="Repository name" autocomplete="off" class="mt-1 w-full max-w-sm" />

      <RepositoryTable v-if="rows.length" class="mt-3" :rows="visibleRows" :busy="busy" @toggle="onToggle" />
      <p v-else-if="settings" id="repo-empty" class="mt-3 text-xs text-muted">Connect the Forge Apps through setup, then refresh access to discover repositories.</p>

      <p class="mt-3 text-xs text-muted">Forge checks existing issues and pull requests before starting work. If access is lost, new work pauses and issue history is kept.</p>
    </section>
  </div>
  <ConfirmDialog
    :open="disableTarget !== null"
    :title="disableTarget ? `Disable automation for ${disableTarget.name}?` : ''"
    text="Forge stops picking up issues in this repository. Existing pull requests, branches and issue history are kept."
    confirm-label="Disable"
    :require-reason="false"
    @confirm="confirmDisable"
    @close="disableTarget = null"
  />
</template>
