<script setup lang="ts">
import ApproversForm from '~/components/ApproversForm.vue'
import CapacityForm from '~/components/CapacityForm.vue'
import { summarizeActivity } from '~/utils/activity'
import { warnings, type FactoryView } from '~/utils/factory'
import type { OnboardingView } from '~/utils/onboarding'
import { statusRows, statusSummary, type SettingsSummary } from '~/utils/status'

interface Payload { settings: SettingsSummary; onboarding: OnboardingView; factory: FactoryView }

useHead({ title: 'Settings' })

const api = useForgeApi()
const route = useRoute()

const capacityForm = ref<InstanceType<typeof CapacityForm> | null>(null)
const approversForm = ref<InstanceType<typeof ApproversForm> | null>(null)

// The poll only advances while nothing is being edited or saved, so a
// mid-cycle refresh never overwrites the operator's unsaved numbers.
const poll = useResource<Payload>('settings-general', () => Promise.all([
  api.get<SettingsSummary>('/api/settings?summary=1', { timeout: 30000 }),
  api.get<OnboardingView>('/api/onboarding', { timeout: 30000 }),
  api.get<FactoryView>('/api/factory?section=board', { timeout: 30000 }),
]).then(([settings, onboarding, factory]) => ({ settings, onboarding, factory })), {
  interval: useRuntimeConfig().public.pollInterval,
  allowed: () => !capacityForm.value?.dirty && !capacityForm.value?.saving
    && !approversForm.value?.dirty && !approversForm.value?.saving,
})

const now = useClock()
onMounted(poll.start)

const settingsView = computed(() => poll.data.value?.settings ?? null)
const onboardingView = computed(() => poll.data.value?.onboarding ?? null)
const factoryView = computed(() => poll.data.value?.factory ?? null)

const rows = computed(() => statusRows({ factory: factoryView.value, settings: settingsView.value, onboarding: onboardingView.value }, now.value))
const summary = computed(() => statusSummary(rows.value))
const banners = computed(() => warnings({ factory: factoryView.value, onboarding: onboardingView.value }, now.value, route.path))
const activity = computed(() => summarizeActivity(factoryView.value, now.value))

const continueSetup = computed(() => !!onboardingView.value && onboardingView.value.phase !== 'active')
const locked = computed(() => onboardingView.value?.phase !== 'active')
const LOCKED_REASON = 'Use guided setup until the factory has started'

// A successful save already carries the fresh FactoryView; folding it
// straight into the poll's payload updates the status rows and the
// "· n working" suffix immediately, rather than waiting out the poll
// interval (during which `allowed` was gating on this form anyway).
// Routed through poll.replace so an in-flight poll from before the save
// can't clobber it and the change signature stays in sync.
function onSaved(view: FactoryView) {
  if (!poll.data.value) return
  poll.replace({ ...poll.data.value, factory: view })
}
</script>

<template>
  <div>
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <p class="text-xs uppercase tracking-wide text-muted">Settings</p>
        <h1 class="text-3xl font-semibold text-white">General</h1>
      </div>
      <NuxtLink v-if="continueSetup" to="/onboarding" class="text-sm text-sky-200 hover:underline">Continue setup</NuxtLink>
    </div>

    <!-- One message block, like the Factory page: what needs the operator's
         attention outranks the fact that the last poll failed, and the two
         are never stacked. -->
    <div v-if="banners.length" class="mt-4 flex flex-col gap-2">
      <WarningBanner v-for="warning in banners" :key="warning.kind + warning.text" :warning="warning" />
    </div>
    <p v-else-if="poll.stale.value" role="status" class="mt-4 rounded-xl border border-amber-200/30 bg-panel px-4 py-3 text-sm text-amber-200">
      Could not refresh. Showing the last received data. {{ poll.error.value }}
    </p>

    <section class="mt-4 rounded-xl border border-line bg-panel p-4">
      <h2 class="text-sm font-semibold text-white">Status</h2>
      <p v-if="!poll.data.value" id="status-summary" class="mt-1 text-sm text-muted">{{ poll.error.value ? 'Could not load the status: ' + poll.error.value : 'Loading status…' }}</p>
      <p v-else id="status-summary" class="mt-1 text-sm" :class="summary.tone === 'warn' ? 'text-amber-200' : summary.tone === 'ok' ? 'text-mint-200' : 'text-muted'">{{ summary.headline }}</p>
      <div v-if="summary.attention.length" class="mt-2 divide-y divide-line">
        <StatusRow v-for="entry in summary.attention" :key="entry.id" :row="entry" />
      </div>
      <details v-if="poll.data.value" class="mt-3 text-sm">
        <summary class="cursor-pointer text-xs text-muted hover:text-white">Show details</summary>
        <div class="mt-2 divide-y divide-line">
          <StatusRow v-for="entry in rows" :key="entry.id" :row="entry" />
        </div>
      </details>
    </section>

    <details :open="route.hash === '#worker-capacity'" id="worker-capacity" class="mt-4 scroll-mt-32 rounded-xl border border-line bg-panel p-4">
      <summary class="cursor-pointer text-sm font-semibold text-white">Worker capacity</summary>
      <CapacityForm
        v-if="factoryView"
        ref="capacityForm"
        :runtime="factoryView.runtime"
        :revision="factoryView.runtime_revision"
        :working="activity.working"
        :locked="locked"
        :locked-reason="LOCKED_REASON"
        @saved="onSaved"
      />
      <p v-else class="mt-2 text-sm text-muted">{{ poll.data.value ? 'Worker capacity is not available.' : 'Loading…' }}</p>
    </details>

    <section id="approvers" class="mt-4 scroll-mt-32 rounded-xl border border-line bg-panel p-4">
      <h2 class="text-sm font-semibold text-white">Who can approve</h2>
      <ApproversForm
        v-if="factoryView"
        ref="approversForm"
        :runtime="factoryView.runtime"
        :revision="factoryView.runtime_revision"
        :locked="locked"
        :locked-reason="LOCKED_REASON"
        @saved="onSaved"
      />
      <p v-else class="mt-2 text-sm text-muted">{{ poll.data.value ? 'The approver list is not available.' : 'Loading…' }}</p>
    </section>

    <details class="mt-4 rounded-xl border border-line bg-panel p-4">
      <summary class="cursor-pointer text-sm text-muted">Reset factory</summary>
      <p class="mt-2 text-sm text-muted">Stop workers and clear this factory’s setup and connections.</p>
      <NuxtLink to="/reset" class="mt-2 inline-block text-sm text-sky-200 hover:underline">Review reset…</NuxtLink>
    </details>
  </div>
</template>
