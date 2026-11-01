<script setup lang="ts">
import { accountNeedsOperator, codexDeviceLink, elapsedTime, githubLink, setupPath, stageStatus } from '~/utils/installation'
useHead({ title: 'Installation progress' })
const { poll, view, headline, busy, actionError, act } = useInstallation()
const localTime = useLocalTime()
const now = ref(Date.now() / 1000)
let ticker: ReturnType<typeof setInterval> | undefined
onMounted(() => { ticker = setInterval(() => { now.value = Date.now() / 1000 }, 1000) })
onBeforeUnmount(() => clearInterval(ticker))
const elapsed = computed(() => elapsedTime(view.value?.started_at, view.value?.complete ? view.value.updated_at ?? now.value : now.value))
const lastChange = computed(() => elapsedTime(view.value?.updated_at, now.value))
const issueUrl = computed(() => githubLink(view.value?.smoke?.issue_url))
const pullUrl = computed(() => githubLink(view.value?.smoke?.pull_url))
const accountActions = computed(() => view.value?.actions.filter(action => ['connect_account', 'retry_account'].includes(action.id)) ?? [])
const setupActions = computed(() => view.value?.actions.filter(action => !['connect_account', 'retry_account'].includes(action.id)) ?? [])
const deviceUrl = computed(() => codexDeviceLink(view.value?.account?.url))
const accountExpired = computed(() => Boolean(view.value?.account?.expires_at && view.value.account.expires_at <= now.value))
const waitingForYou = computed(() => Boolean(view.value && !view.value.complete && (accountNeedsOperator(view.value) || view.value.preparation?.needs_operator.length)))
const accountConnected = computed(() => view.value?.account?.status === 'connected' || view.value?.preparation?.providers.some(provider => provider.id === 'codex' && provider.connected))
</script>
<template>
  <div class="mx-auto max-w-[920px]">
    <p class="text-xs font-semibold tracking-wider text-mint-200 uppercase">Installation</p>
    <h1 class="mt-2 text-3xl font-semibold">{{ view?.complete ? 'Your factory is ready' : 'Setting up your factory' }}</h1>
    <p class="mt-3 text-muted">Follow setup here. You can leave this page and return while Forge works.</p>
    <UButton class="mt-3" to="/settings/repositories" color="neutral" variant="link">Configure repositories</UButton>
    <p v-if="!view && !poll.error.value" role="status" class="mt-6 text-muted">Loading setup progress…</p>
    <UAlert v-if="poll.error.value" class="mt-6" color="warning" variant="subtle" title="Could not refresh setup progress" :description="view ? 'Showing the last received status. Forge may still be working; retry to check.' : poll.error.value" />
    <UButton v-if="poll.error.value" class="mt-3" color="neutral" variant="outline" @click="poll.refresh()">Refresh progress</UButton>
    <template v-if="view?.available">
      <section class="mt-6 rounded-xl border border-line bg-panel p-5" aria-label="Current setup status">
        <p role="status" class="text-lg font-medium">{{ headline }}</p>
        <div class="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-sm text-muted">
          <span v-if="elapsed">Elapsed {{ elapsed }}</span>
          <span v-if="view.updated_at">Last setup report {{ localTime(view.updated_at, true) }}<template v-if="lastChange"> · {{ lastChange }} ago</template></span>
          <span v-if="view.observed_at">Status checked {{ localTime(view.observed_at, true) }}</span>
        </div>
        <p v-if="!view.complete && !poll.stale.value && lastChange" class="mt-3 text-sm text-muted">Refreshing status automatically.</p>
        <p v-if="waitingForYou" class="mt-3 text-sm text-amber-200">Waiting for you. Complete the steps below so setup can continue.</p>
        <p v-else-if="['queued', 'running', 'waiting'].includes(view.operation?.status ?? '')" class="mt-3 text-sm">{{ view.operation?.status === 'queued' ? 'Setup continuation is queued on the server.' : 'Setup will continue automatically on the server.' }}</p>
        <UAlert v-if="view.service && !view.service.available && !view.complete" class="mt-4" color="warning" variant="subtle" title="Setup service is not responding" description="The progress API is reachable, but the setup service has not reported recently. Continuing may wait until it recovers." />
        <UAlert v-if="actionError" class="mt-4" color="error" variant="subtle" :description="actionError" />
        <div v-if="setupActions.length" class="mt-4 flex flex-wrap gap-3">
          <UButton v-for="action in setupActions" :key="action.id" :loading="busy" :disabled="busy" @click="act(action)">{{ action.label }}</UButton>
        </div>
        <UButton v-if="view.complete" class="mt-4" to="/">Open Activity</UButton>
      </section>
      <section v-if="view.account || accountActions.length" class="mt-6 rounded-xl border border-line bg-panel p-5" aria-label="Connect Codex">
        <h2 class="text-lg font-semibold">{{ accountConnected ? 'Codex is connected' : 'Connect your Codex account' }}</h2>
        <template v-if="!accountConnected">
          <template v-if="deviceUrl && view.account?.code && !accountExpired">
            <p class="mt-2 text-sm">Open the sign-in page, enter this one-time code, and approve the connection. Forge will detect completion automatically.</p>
            <p class="mt-4"><code class="select-all rounded-lg border border-line bg-base px-4 py-2 text-xl tracking-widest" aria-label="One-time sign-in code">{{ view.account.code }}</code></p>
            <p v-if="view.account.expires_at" class="mt-3 text-sm text-muted">Expires {{ localTime(view.account.expires_at, true) }}.</p>
            <UButton class="mt-4" :to="deviceUrl" target="_blank" rel="noopener noreferrer">Open Codex sign-in</UButton>
          </template>
          <p v-else-if="view.account?.status === 'account_unavailable'" class="mt-2 text-sm text-amber-200">Forge cannot check the coding account right now. Another login may be in progress, or the account service may be unavailable. Setup progress remains visible; this page will check again.</p>
          <p v-else-if="accountExpired || view.account?.status === 'account_login_failed'" class="mt-2 text-sm text-amber-200">This sign-in attempt expired or failed. Start a new sign-in using the retry button.</p>
          <p v-else-if="accountActions.length" class="mt-2 text-sm">Start sign-in here, then approve the connection in your browser.</p>
          <p v-else class="mt-2 text-sm text-muted">Forge is checking your Codex connection. This page will update automatically.</p>
          <div v-if="accountActions.length" class="mt-4 flex flex-wrap gap-3">
            <UButton v-for="action in accountActions" :key="action.id" :loading="busy" :disabled="busy" @click="act(action)">{{ action.label }}</UButton>
          </div>
        </template>
        <p v-else class="mt-2 text-sm text-muted">Forge can use this account for the installation test.</p>
      </section>
      <section v-if="view.preparation?.needs_operator.length" class="mt-6 rounded-xl border border-amber-200/30 bg-panel p-5" aria-label="Repository setup needs attention">
        <h2 class="text-lg font-semibold">Repository setup is waiting for you</h2>
        <ul class="mt-4 space-y-4">
          <li v-for="item in view.preparation.needs_operator" :key="item.repository.id">
            <h3 class="break-all font-medium">{{ item.repository.name }}</h3>
            <p class="mt-1 text-sm text-muted">{{ item.reason }}</p>
            <UButton class="mt-2" :to="setupPath(item.path) || '/settings/repositories'" color="neutral" variant="outline">Review repository setup</UButton>
          </li>
        </ul>
        <p class="mt-4 text-sm text-muted">Changes to GitHub rules require the local Forge CLI, which holds your administrative access. Follow the repository's setup instructions, then return here and choose Continue setup to check again.</p>
      </section>
      <section class="mt-6 overflow-hidden rounded-xl border border-line bg-panel" aria-label="Installation checklist">
        <h2 class="border-b border-line px-5 py-4 text-lg font-semibold">Setup checklist</h2>
        <ol class="divide-y divide-line">
          <li v-for="stage in view.stages" :key="stage.id" class="flex items-start gap-4 px-5 py-4">
            <span aria-hidden="true" class="mt-0.5 text-mint-200">{{ ['complete', 'completed', 'passed'].includes(stage.status) ? '✓' : ['failed', 'blocked', 'error'].includes(stage.status) ? '!' : '○' }}</span>
            <div class="min-w-0 flex-1"><h3 class="font-medium">{{ stage.label }}</h3><p v-if="stage.reason || stage.error" class="mt-1 break-words text-sm text-muted">{{ stage.reason || stage.error }}</p></div>
            <span class="shrink-0 text-sm" :class="['failed', 'blocked', 'error'].includes(stage.status) ? 'text-amber-200' : 'text-muted'">{{ stageStatus(stage.status) }}</span>
          </li>
        </ol>
      </section>
      <p v-if="view.repositories?.length" class="mt-4 text-sm text-muted">Repositories to enable: {{ view.repositories.map(repo => repo.name).join(', ') }}</p>
      <section v-if="view.smoke" class="mt-6 rounded-xl border border-line bg-panel p-5" aria-label="First merge test">
        <h2 class="text-lg font-semibold">First merge test</h2>
        <p class="mt-2 text-sm text-muted">Forge completes a test issue through Build, Review, Audit and Merge before enabling your selected repositories.</p>
        <p class="mt-3 font-medium" data-testid="installation-smoke-phase">{{ view.smoke.status === 'smoke_triage_failed' ? 'Test planning needs attention' : stageStatus(view.smoke.phase || view.smoke.status) }}</p>
        <p v-if="view.smoke.reason || view.smoke.operator_status?.reason" class="mt-2 break-words text-sm">{{ view.smoke.reason || view.smoke.operator_status?.reason }}</p>
        <div class="mt-4 flex flex-wrap gap-3">
          <UButton v-if="issueUrl" :to="issueUrl" target="_blank" color="neutral" variant="outline">View test issue</UButton>
          <UButton v-if="pullUrl" :to="pullUrl" target="_blank" color="neutral" variant="outline">View pull request</UButton>
          <UButton to="/" color="neutral" variant="link">View factory activity</UButton>
        </div>
      </section>
    </template>
    <section v-else-if="view" class="mt-6 rounded-xl border border-line bg-panel p-5">
      <h2 class="font-semibold">No installation in progress</h2>
      <p class="mt-2 text-sm text-muted">There is no saved installation to continue on this server.</p>
      <UButton class="mt-4" to="/onboarding" color="neutral" variant="outline">Open setup</UButton>
    </section>
  </div>
</template>
