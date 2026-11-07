<script setup lang="ts">
import { ForgeApiError } from '~/composables/useForgeApi'
import { RESETTING_SCREEN, resetComplete, resetStep, type OnboardingView } from '~/utils/onboarding'

// Reset Forge (contracts §3.8, "Reset"). The page is the confirmation: it
// says exactly what a reset does before the one danger button.
useHead({ title: 'Reset' })

const STEPS = [
  ['01', 'Stop all Forge-managed work', 'Pause scheduling and terminate every owned worker.'],
  ['02', 'Discard unfinished local work', 'Remove disposable workspaces and pending factory work.'],
  ['03', 'Clear the current setup', 'Remove selections, settings and Forge-owned credential copies.'],
  ['04', 'Keep your GitHub work', 'Issues, commits, branches, PRs and App registrations remain.'],
]

const api = useForgeApi()
const state = ref<OnboardingView | null>(null)
const busy = ref(false)
const actionError = ref('')
// The generation this page asked to reset: once the server reports a
// different one, the cleanup it promised is done.
const resetGeneration = ref<string | null>(null)

async function load() {
  const next = await api.get<OnboardingView>('/api/onboarding', { timeout: 30000 })
  if (next.phase === 'resetting') resetGeneration.value = next.generation
  state.value = next
  return next
}

const interval = Math.min(3000, useRuntimeConfig().public.pollInterval)
const poll = usePoll(load, {
  interval,
  // Contracts §4: poll only while something is running.
  allowed: () => !busy.value && (Boolean(state.value?.operation) || state.value?.phase === 'resetting'),
})

onMounted(async () => {
  await $fetch('/_ui/setup-cookie').catch(() => {})
  poll.start()
  // The poll only runs while a reset is in flight, so the first read has to
  // be made here.
  try {
    await load()
  } catch (error: unknown) {
    actionError.value = error instanceof ForgeApiError ? error.message : 'Setup state is unavailable.'
  }
})

const resetting = computed(() => state.value?.phase === 'resetting')
const complete = computed(() => Boolean(state.value && resetComplete(state.value, resetGeneration.value)))
const notice = computed(() => actionError.value || state.value?.error || poll.error.value || '')

async function reset() {
  if (busy.value || !state.value) return
  busy.value = true
  actionError.value = ''
  resetGeneration.value = state.value.generation
  try {
    await api.act('/api/onboarding', { action: 'reset', generation: state.value.generation })
    // Loaded directly, not through poll.refresh(): the poll is only allowed
    // once the state it would read says a reset is running.
    await load()
  } catch (error: unknown) {
    actionError.value = error instanceof ForgeApiError ? error.message : 'Forge did not accept the action.'
  } finally {
    busy.value = false
  }
}

// Back to the page that led here (Settings or setup), not to Activity, which
// may not exist yet and would bounce to onboarding.
const router = useRouter()
function cancel() {
  if (window.history.state?.back) router.back()
  else navigateTo('/settings/github')
}
</script>

<template>
  <div class="mx-auto max-w-[780px]">
    <p v-if="notice" role="alert" class="mb-4 text-amber-200">{{ notice }}</p>

    <template v-if="resetting">
      <p class="text-xs font-semibold tracking-[1.4px] text-mint-200 uppercase">{{ RESETTING_SCREEN.eyebrow }}</p>
      <h1 class="mt-2 text-[32px] leading-tight font-semibold tracking-tight">{{ RESETTING_SCREEN.title }}</h1>
      <p class="mt-2 text-muted">{{ RESETTING_SCREEN.intro }}</p>
      <div class="mt-6 flex items-center gap-4 rounded-xl border border-line bg-panel px-5 py-4">
        <span class="size-3.5 rounded-full border border-line border-t-mint-200 motion-safe:animate-spin" aria-hidden="true" />
        <span role="status">{{ resetStep(state?.operation) }}</span>
      </div>
      <p class="mt-4 text-[13px] text-muted">{{ RESETTING_SCREEN.note }}</p>
    </template>

    <template v-else-if="complete">
      <p class="text-xs font-semibold tracking-[1.4px] text-mint-200 uppercase">Reset complete</p>
      <h1 class="mt-2 text-[32px] leading-tight font-semibold tracking-tight">Forge has been reset</h1>
      <p class="mt-2 text-muted">Cleanup is verified. Your GitHub work has been preserved.</p>
      <p class="mt-4">Old GitHub Apps remain registered. You can remove them on GitHub separately.</p>
      <UButton class="mt-4" color="neutral" variant="outline" to="/onboarding">Set up Forge →</UButton>
    </template>

    <template v-else>
      <p class="text-xs font-semibold tracking-[1.4px] text-mint-200 uppercase">Reset Forge</p>
      <h1 class="mt-2 text-[32px] leading-tight font-semibold tracking-tight">Start fresh</h1>
      <p class="mt-2 text-muted">Stop this factory and return to a blank setup.</p>

      <ul class="my-6 list-none p-0">
        <li v-for="[index, title, detail] in STEPS" :key="index" class="flex gap-4 border-b border-line py-4 last:border-b-0">
          <span class="min-w-5 text-muted">{{ index }}</span>
          <div>
            <strong class="font-medium">{{ title }}</strong>
            <small class="mt-1 block text-[13px] text-muted">{{ detail }}</small>
          </div>
        </li>
      </ul>
      <p class="text-[13px] text-muted">Previous lifecycle history is archived. Shared credentials and unrelated host processes remain untouched.</p>

      <div class="mt-8 flex flex-wrap items-center justify-between gap-4 border-t border-line pt-6">
        <UButton color="neutral" variant="outline" @click="cancel">Cancel</UButton>
        <UButton color="warning" :loading="busy" :disabled="busy || !state" @click="reset">Stop and reset Forge</UButton>
      </div>
    </template>
  </div>
</template>
