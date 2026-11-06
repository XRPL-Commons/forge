<script setup lang="ts">
import type { Harness, OnboardingView } from '~/utils/onboarding'

// Account recovery shares the installation's durable device login.
useHead({ title: 'Coding accounts' })

const api = useForgeApi()
const selected = ref<Harness | null>(null)
const open = ref(false)

// Contracts §4: the onboarding poll is 3 s. The e2e harness lowers the
// shared knob to make poll-driven assertions fast; never poll slower than
// the contract's 3 s.
const interval = Math.min(3000, useRuntimeConfig().public.pollInterval)
// A poll never lands while the dialog is open, so it cannot reset a copy
// button under the operator's hands.
const poll = useResource<OnboardingView>('/api/onboarding', path => api.get<OnboardingView>(path, { timeout: 30000 }), { interval, allowed: () => !open.value })

onMounted(async () => {
  // The cookie POST /api/onboarding {action:'github'} needs (contracts §1):
  // a no-op when the server middleware already set one, and the only thing
  // that sets it after a client-side route change into this page.
  await $fetch('/_ui/setup-cookie').catch(() => {})
  poll.start()
})

const state = poll.data
const accountPoll = useResource<{ operation: { status?: string }; browser_providers: string[] }>('/api/accounts', path => api.get(path), {
  interval, allowed: () => state.value?.phase === 'active',
})
onMounted(() => accountPoll.start())
const browserSupported = (id: string) => state.value?.phase === 'active' && accountPoll.data.value?.browser_providers?.includes(id) === true
const accountOperation = computed(() => accountPoll.data.value?.operation.status)
const harnesses = computed(() => state.value?.harnesses ?? [])
// §3.7: the error notice belongs to the page, and only to these two checks.
const notice = computed(() => {
  const checks = state.value?.checks ?? {}
  return checks.provider === 'failed' || checks.runner === 'failed' ? state.value?.error ?? '' : ''
})

// Live account health (the same rows the global alert bar reads). A
// signed-out account leads the page with its reconnect action.
const route = useRoute()
const { alerts } = useCodingAccounts()
const alertFor = (id: string) => alerts.value.find(alert => alert.provider === id) ?? null
function reconnect(provider: string) {
  const harness = harnesses.value.find(item => item.id === provider)
  if (harness) openDialog(harness)
}
// The alert bar deep-links to #account-<provider>; the rows render after
// the first poll, so scroll once they exist.
watch(() => harnesses.value.length, async (count) => {
  if (import.meta.server || !count || !route.hash) return
  await nextTick()
  document.getElementById(route.hash.slice(1))?.scrollIntoView({ block: 'center' })
}, { immediate: true })

function openDialog(harness: Harness) {
  selected.value = harness
  open.value = true
}
</script>

<template>
  <div class="max-w-[780px]">
    <NuxtLink to="/settings/connections" class="mb-4 inline-block text-sm text-sky-200 hover:underline">← Connections</NuxtLink>
    <div class="flex flex-wrap items-end justify-between gap-3">
      <div>
        <p class="text-xs uppercase tracking-wide text-muted">Settings</p>
        <h1 class="text-3xl font-semibold text-white">Coding accounts</h1>
      </div>
      <NuxtLink v-if="state && state.phase !== 'active'" to="/onboarding" class="text-sm text-sky-200 hover:underline">Continue setup</NuxtLink>
    </div>
    <p class="mt-2 text-muted">Connect Codex here. Other providers currently use the Forge CLI.</p>

    <UAlert
      v-for="alert in alerts"
      :key="alert.provider"
      class="mt-5"
      :color="alert.color"
      variant="subtle"
      :icon="alert.status === 'sign_in_expired' ? 'i-lucide-key-round' : 'i-lucide-triangle-alert'"
      :title="alert.title"
      :description="alert.description"
      :data-settings-account-alert="alert.provider"
      :actions="[{ label: browserSupported(alert.provider) ? alert.actionLabel : 'CLI instructions', color: alert.color, variant: 'solid', onClick: () => reconnect(alert.provider) }]"
    />

    <UAlert v-if="notice" class="mt-5" color="warning" variant="subtle" :description="notice" />

    <p v-if="!state" class="mt-6 text-muted">{{ poll.error.value || 'Loading setup…' }}</p>

    <template v-else>
      <div class="mt-6 overflow-hidden rounded-xl border border-line bg-panel">
        <HarnessCard
          v-for="harness in harnesses"
          :key="harness.id"
          :harness="harness"
          :activity="state.activity"
          :alert="alertFor(harness.id)"
          :operation="harness.id === 'codex' ? accountOperation : undefined"
          :browser="browserSupported(harness.id)"
          @open="openDialog"
        />
      </div>
      <p class="mt-4 text-[13px] text-muted">Forge uses available accounts across build, review and audit.</p>
    </template>

    <LazyCodexAccountDialog v-if="selected && browserSupported(selected.id) && open" :harness="selected" :reconnect="alertFor('codex')?.status === 'sign_in_expired'" :open="open" @close="open = false; poll.refresh()" />
    <HarnessDialog v-else :harness="selected" :open="open" @close="open = false" />
  </div>
</template>
