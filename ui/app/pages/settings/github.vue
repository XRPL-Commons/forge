<script setup lang="ts">
import {
  actionMessage, connectionCards, lastRefreshText,
  type OnboardingSummary, type SettingsView,
} from '~/utils/repositories'

// App connections and access refresh; repository automation has its own tab.
useHead({ title: 'GitHub access' })

const REFRESH_DONE = 'Access refreshed.'
const REFRESH_UNFINISHED = 'The access check did not finish. Refresh access to try again.'
const REFRESH_FAILED = 'Could not refresh access. Try again.'
const REFRESH_DEADLINE = 90000

interface Snapshot { settings: SettingsView; onboarding: OnboardingSummary }

const api = useForgeApi()
const localTime = useLocalTime()

const notice = ref('')
const noticeError = ref(false)
const refreshing = ref(false)
const refreshNote = ref('')
let refreshStarted: number | null = null
let refreshTimer: ReturnType<typeof setTimeout> | undefined
let noticeTimer: ReturnType<typeof setTimeout> | undefined

const poll = useResource<Snapshot>('settings-github', async () => {
  const [settings, onboarding] = await Promise.all([
    api.get<SettingsView>('/api/settings?summary=1', { timeout: 30000 }),
    api.get<OnboardingSummary>('/api/onboarding', { timeout: 30000 }),
  ])
  return { settings, onboarding }
}, {
  interval: useRuntimeConfig().public.pollInterval,
  onChange: observeRefresh,
})

onMounted(() => poll.start())
onBeforeUnmount(() => { clearTimeout(refreshTimer); clearTimeout(noticeTimer) })

const settings = computed(() => poll.data.value?.settings ?? null)
const onboarding = computed(() => poll.data.value?.onboarding ?? null)
const settingUp = computed(() => !!onboarding.value && onboarding.value.phase !== 'active')

const syncStatus = computed(() => settings.value ? lastRefreshText(settings.value, localTime) : 'Loading settings…')
const cards = computed(() => settings.value ? connectionCards(settings.value, onboarding.value, localTime) : [])
const syncing = computed(() => refreshing.value || settings.value?.sync.status === 'running')

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

    <UAlert v-if="notice && noticeError" id="notice" class="mt-4" role="alert" color="error" variant="subtle" :description="notice" />
    <p v-else-if="notice" id="notice" role="status" class="mt-4 rounded-xl border border-line bg-panel px-4 py-3 text-sm text-white">{{ notice }}</p>

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
          icon="i-lucide-refresh-cw"
          :ui="{ leadingIcon: syncing ? 'animate-spin' : '' }"
          @click="refreshAccess"
        />
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

    <p class="mt-4 text-sm text-muted">Manage automation, deployment tracking and test databases in <NuxtLink to="/settings/repositories" class="text-sky-200 underline">Repositories</NuxtLink>.</p>
  </div>
</template>
