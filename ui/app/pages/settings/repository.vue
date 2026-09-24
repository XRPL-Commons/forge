<script setup lang="ts">
import { ForgeApiError } from '~/composables/useForgeApi'
import type { FactoryView } from '~/utils/factory'
import {
  accessRows, actionMessage, branchText, checkedAtText, deploymentCard, displayStatus, hintText, otherRulesText,
  protectionReport, repoDescription, repoHeading, repoStage, requirements, rulesetFilename,
  rulesetHref, scopeText, statusPillText,
  type FactoryDeployment, type RepositoriesView, type RepositoryRow,
} from '~/utils/repositories'

// Settings · Repository setup (contracts §3.6): pick a repository, give the
// four Apps access to it, put the GitHub review rules in place, then enable
// automation. One status pill and one description say where the operator is;
// the step strip (RepositoryStage) says what is left.
useHead({ title: 'Prepare repository' })

const UNAVAILABLE = 'Repository setup is unavailable. Return to GitHub access and refresh.'
const REFRESH_DONE = 'Access refreshed.'
const REFRESH_UNFINISHED = 'The access check did not finish. Refresh access to try again.'
const REFRESH_FAILED = 'Could not refresh access. Try again.'
const REFRESH_DEADLINE = 90000

const api = useForgeApi()
const route = useRoute()

const repoId = computed(() => {
  const raw = route.query.repo_id
  const value = Array.isArray(raw) ? raw[0] : raw
  return value === undefined || value === null || value === '' ? null : Number(value)
})

// Any POST is in flight (check, enable): polling yields until it lands.
const busy = ref(false)
const enabling = ref(false)
const downloaded = ref(false)
const error = ref('')
const query = ref('')
const missingOpen = ref(false)
const detailsOpen = ref(false)
const refreshing = ref(false)
const accessNote = ref('')
// One auto-check per observation: `revision:protection_status:observed_at`.
const autoCheckKey = ref('')
const deployError = ref('')
const rejecting = ref(false)
const deployments = ref<Record<string, FactoryDeployment>>({})
let loadingDeployments = false
let refreshStarted: number | null = null
let refreshTimer: ReturnType<typeof setTimeout> | undefined

const poll = usePoll<RepositoriesView>(async () => {
  const id = repoId.value
  const view = await api.get<RepositoriesView>('/api/repositories' + (id === null ? '' : '?repo_id=' + id), { timeout: 30000 })
  // A view without the requested repository is not a page: the operator is
  // sent back to GitHub access rather than shown an empty shell.
  if (!Array.isArray(view.repositories) || (id !== null && !view.repositories.length)) throw new ForgeApiError(200, UNAVAILABLE)
  // What Forge last observed of a tracked deployment lives on the factory
  // view, not on this one. It is read beside the page, never in front of it:
  // the card renders from the profile whether or not it answers.
  if (view.repositories.find(candidate => candidate.id === id)?.deployment?.status === 'confirmed') void loadDeployments()
  return view
// Repository setup wants a faster tick than the rest of the app (an access
// check lands within seconds), but never slower than the configured one —
// which is how the e2e harness's 300ms reaches this page too.
}, { interval: Math.min(3000, useRuntimeConfig().public.pollInterval), allowed: () => !busy.value, onChange: observe })

// The observation carries an age ("deploy succeeded 12 min ago"), which the
// server cannot know; it appears once mounted and ticks with the clock.
const now = ref(0)
let ticker: ReturnType<typeof setInterval> | undefined
onMounted(() => {
  poll.start()
  now.value = Math.floor(Date.now() / 1000)
  ticker = setInterval(() => { now.value = Math.floor(Date.now() / 1000) }, 30000)
})
onBeforeUnmount(() => { clearTimeout(refreshTimer); clearInterval(ticker) })

// A different repository is a different page: drop the per-repository state
// and load again immediately instead of waiting for the next tick.
watch(repoId, async () => {
  downloaded.value = false
  autoCheckKey.value = ''
  error.value = ''
  deployError.value = ''
  // A refresh started before the navigation resolves with the previous
  // route's payload (`usePoll` coalesces in-flight loads), which holds no row
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
const prepared = computed(() => ['ready', 'needs_setup'].includes(shown.value ?? ''))
const canEnable = computed(() => live.value && !!row.value && !row.value.enabled)
const stage = computed(() => repoStage(row.value, live.value))
const accessNeeded = computed(() => row.value?.protection_status === 'access_needed')

const pickerRows = computed(() => {
  const needle = query.value.trim().toLowerCase()
  return (view.value?.repositories ?? []).filter(candidate => !candidate.enabled && candidate.name.toLowerCase().includes(needle))
})
const connections = computed(() => accessRows(view.value?.connections, row.value))
// Hidden once every App can reach the chosen repository; on the picker it is
// the way out when no repository can be offered yet.
const showAccess = computed(() => repoId.value === null ? !pickerRows.value.length || missingOpen.value : !row.value?.access_ready)
const accessDescription = computed(() => row.value
  ? `Give each App access to ${row.value.name}. After making changes on GitHub, refresh access here.`
  : 'Use Manage access to add your repository to a Forge App, then refresh access here.')

const failed = computed(() => requirements(report.value).filter(requirement => !requirement.passed))
const editUrl = computed(() => report.value?.edit_url ?? '')
const canDownload = computed(() => ['ready', 'needs_setup'].includes(row.value?.protection_status ?? '') && !busy.value)
const downloadHref = computed(() => row.value && view.value && canDownload.value ? rulesetHref(row.value, view.value.revision) : undefined)
// A plain boolean would be typed as a real disabled state; this link is
// never disabled, it only says so to assistive technology.
const downloadAriaDisabled = computed<'true' | 'false'>(() => (canDownload.value ? 'false' : 'true'))
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
  if (!current) return
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
    await nextTick()
    document.getElementById('status')?.focus()
  } catch (failure: unknown) {
    error.value = actionMessage(failure, 'Could not enable this repository.')
    busy.value = false
    await poll.refresh()
  } finally {
    busy.value = false
    enabling.value = false
  }
}

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
</script>

<template>
  <div>
    <p class="text-xs uppercase tracking-wide text-muted">Settings › Repo setup</p>
    <h1 class="text-3xl font-semibold text-white">{{ repoId === null ? 'Add repository' : repoHeading(row, live) }}</h1>
    <p v-if="repoId === null" class="mt-1 text-sm text-muted">Choose a repository. Forge will guide you through the setup.</p>

    <RepositoryStage class="mt-4" :stage="stage" :activation-available="live" />

    <p v-if="error" id="error" role="alert" class="mt-4 rounded-xl border border-rose-200/40 px-4 py-3 text-sm text-rose-200">{{ error }}</p>

    <!-- Picker: no repository chosen yet. -->
    <section v-if="repoId === null" class="mt-4 rounded-xl border border-line bg-panel p-4">
      <h2 class="text-sm font-semibold text-white">Choose your repository</h2>
      <label for="repo-search" class="mt-3 block text-xs text-muted">Find by name</label>
      <UInput id="repo-search" v-model="query" type="search" placeholder="owner/repository" autocomplete="off" class="mt-1 w-full max-w-sm" />
      <ul v-if="pickerRows.length" id="repo-options" class="mt-3 flex flex-col gap-1 text-sm">
        <li v-for="candidate in pickerRows" :key="candidate.id" :data-key="candidate.id">
          <NuxtLink :to="`/settings/repository?repo_id=${candidate.id}`" class="break-all text-sky-200 underline-offset-4 hover:underline">{{ candidate.name }}</NuxtLink>
        </li>
      </ul>
      <p v-else id="no-repositories" class="mt-3 text-xs text-muted">No matching repositories yet.</p>

      <UCollapsible v-model:open="missingOpen" class="mt-3">
        <UButton color="neutral" variant="link" class="px-0 text-xs">Repository missing?</UButton>
        <template #content>
          <p class="pt-2 text-xs text-muted">Give a Forge App access to the repository on GitHub using the links below, then refresh access. It will appear here once Forge can see it.</p>
        </template>
      </UCollapsible>
    </section>

    <!-- App access: the four Apps and their reach into this repository. -->
    <section v-if="showAccess" id="app-access" class="mt-4 rounded-xl border border-line bg-panel p-4">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <h2 class="text-sm font-semibold text-white">Connect the Forge Apps</h2>
        <UButton
          color="neutral"
          variant="ghost"
          aria-label="Refresh GitHub access"
          title="Refresh GitHub access"
          :disabled="!view"
          :aria-busy="syncing ? 'true' : undefined"
          @click="refreshAccess"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" class="h-5 w-5" :class="syncing ? 'animate-spin' : ''" aria-hidden="true" focusable="false">
            <path d="M20 7v5h-5M4 17v-5h5" /><path d="M6.1 6.1A8 8 0 0 1 20 12M4 12a8 8 0 0 0 13.9 5.9" />
          </svg>
        </UButton>
      </div>
      <p class="mt-1 text-xs text-muted">{{ accessDescription }}</p>
      <p v-if="accessNote" id="access-result" role="status" class="mt-1 text-xs text-sky-200">{{ accessNote }}</p>
      <ul id="app-connections" class="mt-3 flex flex-col gap-2">
        <li v-for="connection in connections" :key="connection.role" :data-key="connection.role" class="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-line px-3 py-2 text-xs">
          <div>
            <strong class="text-sm font-medium text-white">{{ connection.label }}</strong>
            <p v-if="!connection.ready" class="text-muted">{{ connection.message }}</p>
          </div>
          <span v-if="connection.ready" class="text-mint-200">Connected ✓</span>
          <NuxtLink
            v-else
            :to="connection.action.href"
            :target="connection.action.external ? '_blank' : undefined"
            :rel="connection.action.external ? 'noopener noreferrer' : undefined"
            :external="connection.action.external"
            class="text-sky-200 underline-offset-4 hover:underline"
          >{{ connection.action.label }}</NuxtLink>
        </li>
      </ul>
    </section>

    <template v-if="row">
      <!-- Summary: the one status pill and the one description of this screen. -->
      <section id="repository-summary" class="mt-4 rounded-xl border border-line bg-panel p-4">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 class="break-all text-lg font-semibold text-white">{{ row.name }}</h2>
            <p class="mt-0.5 text-xs text-muted">{{ branchText(report) }}</p>
          </div>
          <div class="flex items-center gap-3">
            <UBadge id="status" tabindex="-1" :color="displayReady ? 'primary' : 'warning'" variant="subtle">{{ statusPillText(row, live) }}</UBadge>
            <NuxtLink to="/settings/repository" class="text-xs text-sky-200 underline-offset-4 hover:underline">Change</NuxtLink>
          </div>
        </div>
        <p id="description" class="mt-2 text-sm text-muted">{{ repoDescription(row, live) }}</p>

        <ul v-if="shown === 'needs_setup' && failed.length" id="requirements" class="mt-3 flex flex-col gap-2">
          <li v-for="requirement in failed" :key="requirement.id" class="flex gap-2 text-xs">
            <span aria-hidden="true" class="text-amber-200">○</span>
            <div>
              <strong class="text-sm font-medium text-white">{{ requirement.label }}</strong>
              <p class="text-muted">{{ requirement.detail }}</p>
            </div>
          </li>
        </ul>

        <p v-if="hint" id="hint" class="mt-3 text-xs text-amber-200">{{ hint }}</p>
      </section>

      <!-- Applying the review rules on GitHub. -->
      <section v-if="!displayReady && prepared" id="steps" class="mt-4 rounded-xl border border-line bg-bg p-4">
        <ol class="flex flex-col gap-4">
          <li v-if="!editUrl" id="download-step">
            <h3 class="text-sm font-semibold text-white">Download the review rules</h3>
            <p class="mt-0.5 text-xs text-muted">A ready-to-import file. No editing or administrator token needed.</p>
            <a
              id="download"
              class="mt-2 inline-block rounded-full border border-mint-200 px-3 py-1 text-xs text-mint-200"
              :href="downloadHref"
              :download="rulesetFilename(row)"
              :aria-disabled="downloadAriaDisabled"
              @click="downloaded = true"
            >{{ downloaded ? 'Download again' : 'Download ruleset' }}</a>
          </li>
          <li>
            <h3 id="import-title" class="text-sm font-semibold text-white">{{ editUrl ? 'Update the existing ruleset' : 'Import on GitHub' }}</h3>
            <p id="import-description" class="mt-0.5 text-xs text-muted">
              {{ editUrl
                ? 'A Forge review ruleset already exists. Update its review settings using the requirements above.'
                : 'On GitHub, choose New ruleset → Import a ruleset. Select the file, review the settings and save.' }}
            </p>
            <a
              id="github"
              class="mt-2 inline-block rounded-full border border-line px-3 py-1 text-xs text-sky-200"
              :href="editUrl || row.rules_url"
              target="_blank"
              rel="noopener noreferrer"
            >{{ editUrl ? 'Edit ruleset on GitHub ↗' : 'Open GitHub settings ↗' }}</a>
            <p id="scope" class="mt-2 text-xs text-muted">{{ scopeText(report) }}</p>
          </li>
          <li>
            <h3 class="text-sm font-semibold text-white">Return here to check</h3>
            <p class="mt-0.5 text-xs text-muted">After saving on GitHub, check that the review requirements are in place.</p>
          </li>
        </ol>
      </section>

      <!-- Footer: check, enable, continue. -->
      <div class="mt-4 flex flex-wrap items-center gap-3">
        <UButton
          v-if="!accessNeeded"
          id="check"
          color="neutral"
          variant="ghost"
          aria-label="Refresh repository"
          title="Refresh repository"
          :disabled="busy"
          :aria-busy="checking || busy ? 'true' : undefined"
          @click="check"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" class="h-5 w-5" :class="checking || busy ? 'animate-spin' : ''" aria-hidden="true" focusable="false">
            <path d="M20 7v5h-5M4 17v-5h5" /><path d="M6.1 6.1A8 8 0 0 1 20 12M4 12a8 8 0 0 0 13.9 5.9" />
          </svg>
        </UButton>
        <span id="checked-at" class="text-xs text-muted">{{ checkedAtText(report) }}</span>
        <UButton
          v-if="canEnable && displayReady"
          id="enable"
          color="primary"
          :disabled="busy || !ready"
          class="ms-auto"
          @click="enable"
        >{{ enabling ? 'Enabling…' : 'Enable automation' }}</UButton>
        <UButton
          v-if="!live && displayReady"
          id="continue"
          color="primary"
          variant="outline"
          class="ms-auto"
          :to="ready && !busy ? (view?.continue_url || '/onboarding') : undefined"
          :aria-disabled="String(!ready || busy)"
        >Continue to Forge →</UButton>
      </div>

      <!-- How this repository reaches production: the strategist's reading,
           the operator's decision, and what Forge observes afterwards. -->
      <DeploymentCard
        v-if="deployment"
        :card="deployment"
        :busy="busy"
        :error="deployError"
        @confirm="domain => decide('confirm', { domain })"
        @reject="rejecting = true"
        @reread="decide('reread')"
      />

      <UCollapsible v-model:open="detailsOpen" class="mt-4">
        <UButton color="neutral" variant="link" class="px-0 text-xs">Setup details</UButton>
        <template #content>
          <div class="pt-3 text-xs text-muted">
            <ul class="flex flex-col gap-2">
              <li v-for="requirement in requirements(report)" :key="requirement.id" class="flex gap-2">
                <span aria-hidden="true" :class="requirement.passed ? 'text-mint-200' : 'text-amber-200'">{{ requirement.passed ? '✓' : '○' }}</span>
                <div>
                  <strong class="text-sm font-medium text-white">{{ requirement.label }}</strong>
                  <p>{{ requirement.detail }}</p>
                </div>
              </li>
            </ul>
            <p class="mt-3">Forge reads review requirements using its existing GitHub access. It cannot change your rules.</p>
            <p class="mt-2"><strong class="text-white">Bypass permissions need your review.</strong> GitHub may hide who can bypass a ruleset. Check the bypass list on GitHub; a successful check here does not certify that list.</p>
            <p class="mt-2">{{ otherRulesText(report) }}</p>
            <ul v-if="report?.sources?.length" class="mt-2 flex flex-col gap-1">
              <li v-for="source in report.sources" :key="source.url">
                <a :href="source.url" target="_blank" rel="noopener noreferrer" class="text-sky-200 underline-offset-4 hover:underline">{{ source.name }}{{ source.kind === 'ruleset' ? ' · ruleset' : '' }} ↗</a>
              </li>
            </ul>
            <NuxtLink to="/setup-help#github-repository-protection" class="mt-3 inline-block text-sky-200 underline-offset-4 hover:underline">Read the setup guide →</NuxtLink>
          </div>
        </template>
      </UCollapsible>
    </template>

    <ConfirmDialog
      :open="rejecting"
      :title="row ? `Reject the deployment reading for ${row.name}?` : ''"
      text="Forge keeps following this repository's issues. Nothing is saved to settings, and the reading can be requested again."
      confirm-label="Reject"
      reason-label="Why is this reading wrong?"
      :error="deployError"
      @confirm="rejectDeployment"
      @close="rejecting = false"
    />
  </div>
</template>
