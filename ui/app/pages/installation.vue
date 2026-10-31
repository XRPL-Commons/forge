<script setup lang="ts">
import { elapsedTime, githubLink, stageStatus } from '~/utils/installation'
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
</script>
<template>
  <div class="mx-auto max-w-[920px]">
    <p class="text-xs font-semibold tracking-wider text-mint-200 uppercase">Installation</p>
    <h1 class="mt-2 text-3xl font-semibold">{{ view?.complete ? 'Your factory is ready' : 'Setting up your factory' }}</h1>
    <p class="mt-3 text-muted">Follow setup here. You can leave this page and return while Forge works.</p>
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
        <p v-if="['queued', 'running', 'waiting'].includes(view.operation?.status ?? '')" class="mt-3 text-sm">{{ view.operation?.status === 'queued' ? 'Setup continuation is queued on the server.' : 'Setup will continue automatically on the server.' }}</p>
        <UAlert v-if="view.service && !view.service.available && !view.complete" class="mt-4" color="warning" variant="subtle" title="Setup service is not responding" description="The progress API is reachable, but the setup service has not reported recently. Continuing may wait until it recovers." />
        <UAlert v-if="actionError" class="mt-4" color="error" variant="subtle" :description="actionError" />
        <div v-if="view.actions.length" class="mt-4 flex flex-wrap gap-3">
          <UButton v-for="action in view.actions" :key="action.id" :loading="busy" :disabled="busy" @click="act(action)">{{ action.label }}</UButton>
        </div>
        <UButton v-if="view.complete" class="mt-4" to="/">Open Activity</UButton>
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
