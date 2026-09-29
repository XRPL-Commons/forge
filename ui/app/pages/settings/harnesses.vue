<script setup lang="ts">
import type { Harness, OnboardingView } from '~/utils/onboarding'

// Settings · Coding accounts (contracts §3.7). Sign-in happens in a terminal
// on the Forge VM; this page shows what each harness reports and hands over
// the two commands.
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
const harnesses = computed(() => state.value?.harnesses ?? [])
// §3.7: the error notice belongs to the page, and only to these two checks.
const notice = computed(() => {
  const checks = state.value?.checks ?? {}
  return checks.provider === 'failed' || checks.runner === 'failed' ? state.value?.error ?? '' : ''
})

function openDialog(harness: Harness) {
  selected.value = harness
  open.value = true
}
</script>

<template>
  <div class="max-w-[780px]">
    <div class="flex flex-wrap items-end justify-between gap-3">
      <div>
        <p class="text-xs uppercase tracking-wide text-muted">Settings</p>
        <h1 class="text-3xl font-semibold text-white">Coding accounts</h1>
      </div>
      <NuxtLink v-if="state && state.phase !== 'active'" to="/onboarding" class="text-sm text-sky-200 hover:underline">Continue setup</NuxtLink>
    </div>
    <p class="mt-2 text-muted">Manage sign-in from the Forge VM terminal.</p>

    <UAlert v-if="notice" class="mt-5" color="warning" variant="subtle" :description="notice" />

    <p v-if="!state" class="mt-6 text-muted">{{ poll.error.value || 'Loading setup…' }}</p>

    <template v-else>
      <div class="mt-6 overflow-hidden rounded-xl border border-line bg-panel">
        <HarnessCard
          v-for="harness in harnesses"
          :key="harness.id"
          :harness="harness"
          :activity="state.activity"
          @open="openDialog"
        />
      </div>
      <p class="mt-4 text-[13px] text-muted">Connected harnesses and models take turns across all roles. If one is unavailable, Forge uses another.</p>
    </template>

    <HarnessDialog :harness="selected" :open="open" @close="open = false" />
  </div>
</template>
