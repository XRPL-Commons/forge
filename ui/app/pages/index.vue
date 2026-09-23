<script setup lang="ts">
import { ForgeApiError } from '~/composables/useForgeApi'
import {
  COLUMN_ROLES, COLUMN_TITLES, destination, elapsed, groupIssues, isFresh, breakdown, warnings,
  type CardModel, type ColumnRole, type FactoryView, type Warning,
} from '~/utils/factory'

interface Fence { ok: false; reason: string }

useHead({ title: 'Activity' })

const api = useForgeApi()
const route = useRoute()
const toast = useToast()

// True while a confirmation dialog is open or a POST is in flight, so a poll
// never re-renders the page under the operator's hands.
const confirming = ref(false)
// A {ok:false, reason} answer is a state, not a failed request: it replaces
// the payload, so the page shows the fence instead of a stale factory.
const poll = usePoll<FactoryView | Fence>(async () => {
  try {
    return await api.get<FactoryView>('/api/factory', { timeout: 15000 })
  } catch (error: unknown) {
    if (error instanceof ForgeApiError && error.status === 200) return { ok: false as const, reason: error.message }
    throw error
  }
}, {
  interval: useRuntimeConfig().public.pollInterval,
  allowed: () => !confirming.value,
})

const now = ref(Math.floor(Date.now() / 1000))
let ticker: ReturnType<typeof setInterval> | undefined
onMounted(() => {
  poll.start()
  ticker = setInterval(() => { if (!document.hidden) now.value = Math.floor(Date.now() / 1000) }, 1000)
})
onBeforeUnmount(() => clearInterval(ticker))

const fence = computed(() => { const payload = poll.data.value; return payload && payload.ok === false ? payload : null })
const view = computed(() => { const payload = poll.data.value; return payload && payload.ok !== false ? payload : null })
const connectionLost = computed(() => !!poll.error.value && !!view.value)
const fresh = computed(() => isFresh(view.value, now.value))
const observedAt = computed(() => (view.value?.observation as { at?: number } | undefined)?.at ?? null)

const freshness = computed(() => {
  if (!view.value) return 'Connecting…'
  if (poll.error.value) return 'Reconnecting · last known state'
  if (fresh.value && observedAt.value) return `Live · checked ${elapsed(now.value - observedAt.value)} ago`
  return 'Updates delayed · last known state'
})

// At most one message block above the metrics: connection lost -> the
// freshness pill only; otherwise the banner(s); otherwise the operator
// status line. A {ok:false, reason} payload leaves no state to show, so its
// reason is the page's single banner.
const banners = computed<Warning[]>(() => {
  if (fence.value) return [{ kind: 'other', text: fence.value.reason, ...destination('other', route.path) }]
  if (!view.value) {
    const reason = poll.error.value
    return reason ? [{ kind: 'other', text: reason, ...destination('other', route.path) }] : []
  }
  if (connectionLost.value) return []
  return warnings({ factory: view.value }, now.value, route.path)
})

const operator = computed(() => view.value?.operator ?? null)
// When a banner says what the operator status line says, the banner absorbs
// it: the message is rendered exactly once, keeping the warning's own
// remediation link and falling back to the operator action when it has none.
const absorbed = computed(() => {
  const detail = operator.value?.detail
  if (!detail) return null
  return banners.value.find(warning => warning.text === detail) ?? null
})
const showStatusLine = computed(() => !!view.value && !connectionLost.value && !banners.value.length && !!operator.value?.title)

// `operator.action_url` is sometimes off the box (a GitHub settings page,
// the docs): an external destination opens in its own tab, and never hands
// this page's window to whatever it opens.
function isExternal(url: string): boolean { return url.startsWith('http') }

const rows = computed(() => breakdown(view.value, now.value))
const groups = computed(() => groupIssues(view.value, now.value))
const slots = computed(() => view.value?.runtime?.slots ?? {})

function summary(role: ColumnRole, cards: CardModel[]): string {
  const working = cards.filter(card => card.running).length
  const waiting = cards.filter(card => !card.jobId).length
  const between = cards.length - working - waiting
  return [`${working} working`, `${waiting} queued`, ...(between ? [`${between} starting / finishing`] : []), `${slots.value[role] ?? 0} maximum`].join(' · ')
}

const runnerCards = computed(() => [...groups.value.investigating, ...groups.value.internalJobs])
// Expanded whenever the section holds a job the supervisor last reported as
// running, so an active investigation is never hidden behind a disclosure.
const runnersAuto = computed(() => runnerCards.value.some(card => card.running || card.phaseLabel === 'Last seen working'))
const runnersManual = ref<boolean | null>(null)
const runnersOpen = computed(() => runnersManual.value ?? runnersAuto.value)
const completedOpen = ref(false)

const announcement = ref('')
let placement = new Map<string, ColumnRole>()
watch(groups, (next) => {
  const seen = new Map<string, ColumnRole>()
  const moves: string[] = []
  for (const role of COLUMN_ROLES) {
    for (const card of next.columns[role]) {
      seen.set(card.key, role)
      const before = placement.get(card.key)
      if (before && before !== role) moves.push(`Issue ${card.number} moved to ${COLUMN_TITLES[role]}`)
    }
  }
  placement = seen
  if (moves.length) announcement.value = moves.join('. ')
})

const STOP = {
  action: 'stop',
  title: 'Stop this attempt?',
  text: 'This cancels the issue’s active attempt, discards unfinished workspace edits, and resets its runner. Published PRs are preserved.',
  confirmLabel: 'Stop attempt',
}
const RESET = {
  action: 'reset',
  title: 'Reset runner?',
  text: 'This terminates the old execution and discards its disposable workspace. Capacity is released only after teardown is verified.',
  confirmLabel: 'Reset runner',
}

const dialog = ref<{ action: string; title: string; text: string; confirmLabel: string; target: string } | null>(null)
const dialogError = ref<string | null>(null)

function ask(kind: typeof STOP, target: string) {
  dialogError.value = null
  dialog.value = { ...kind, target }
  confirming.value = true
}
function closeDialog() {
  dialog.value = null
  dialogError.value = null
  confirming.value = false
}
async function confirm(reason: string) {
  const open = dialog.value
  if (!open) return
  dialogError.value = null
  try {
    await api.act('/api/factory/action', { action: open.action, target: open.target, reason })
  } catch (error: unknown) {
    dialogError.value = error instanceof Error ? error.message : 'Forge did not accept the action.'
    return
  }
  closeDialog()
  toast.add({ title: 'Action queued for the supervisor.', color: 'success' })
  await poll.refresh()
}
</script>

<template>
  <div>
    <div class="flex flex-wrap items-end justify-between gap-3">
      <div>
        <p class="text-xs uppercase tracking-wide text-muted">Live operations</p>
        <h1 class="text-3xl font-semibold text-white">Factory</h1>
      </div>
      <p
        v-if="!fence"
        id="freshness"
        role="status"
        class="text-xs"
        :class="fresh && !poll.error.value ? 'text-muted' : 'text-amber-200'"
      >{{ freshness }}</p>
    </div>

    <div v-if="banners.length" class="mt-4 flex flex-col gap-2">
      <WarningBanner
        v-for="warning in banners"
        :key="warning.kind + warning.text"
        :warning="absorbed && absorbed.text === warning.text ? absorbed : warning"
        :title="absorbed && absorbed.text === warning.text ? operator?.title : undefined"
      />
    </div>

    <div v-else-if="showStatusLine" class="mt-4 rounded-xl border border-line bg-panel px-4 py-3">
      <h2 class="text-sm font-semibold text-white">{{ operator!.title }}</h2>
      <p class="mt-0.5 text-sm text-muted">{{ operator!.detail }}</p>
      <UButton
        v-if="operator!.action_url && operator!.action_label"
        :to="operator!.action_url"
        :target="isExternal(operator!.action_url) ? '_blank' : undefined"
        :rel="isExternal(operator!.action_url) ? 'noopener noreferrer' : undefined"
        :external="isExternal(operator!.action_url) || undefined"
        size="xs"
        color="neutral"
        variant="outline"
        class="mt-2"
      >{{ operator!.action_label }}</UButton>
    </div>

    <template v-if="view">
      <PipelineBreakdown class="mt-5" :rows="rows" />

      <section id="columns" class="mt-6 grid scroll-mt-32 gap-3 lg:grid-cols-3">
        <StageColumn
          v-for="role in COLUMN_ROLES"
          :id="`stage-${role}`"
          :key="role"
          :title="COLUMN_TITLES[role]"
          :summary="summary(role, groups.columns[role])"
          :cards="groups.columns[role]"
          @stop="ask(STOP, $event)"
        />
      </section>

      <section id="needs-attention" class="mt-6 scroll-mt-32 rounded-xl border border-line bg-bg p-3">
        <h2 class="text-sm font-semibold text-white">Needs attention · {{ groups.attention.length }}</h2>
        <div v-if="groups.attention.length" class="mt-3 grid gap-2.5 sm:grid-cols-2 xl:grid-cols-3">
          <IssueCard v-for="card in groups.attention" :key="card.key" :card="card" @stop="ask(STOP, $event)" />
        </div>
        <p v-else class="mt-2 text-xs text-muted">No issues need your attention.</p>
      </section>

      <section id="finishing" class="mt-4 scroll-mt-32 rounded-xl border border-line bg-bg p-3">
        <h2 class="text-sm font-semibold text-white">Finishing</h2>
        <p class="mt-0.5 text-xs text-muted">Approved work waiting to merge, or being merged.</p>
        <div v-if="groups.finishing.length" class="mt-3 grid gap-2.5 sm:grid-cols-2 xl:grid-cols-3">
          <IssueCard v-for="card in groups.finishing" :key="card.key" :card="card" @stop="ask(STOP, $event)" />
        </div>
        <p v-else class="mt-2 text-xs text-muted">No issues waiting to merge.</p>
      </section>

      <section id="runners" class="mt-4 scroll-mt-32 rounded-xl border border-line bg-bg p-3">
        <details :open="runnersOpen" @toggle="event => runnersManual = (event.target as HTMLDetailsElement).open">
          <summary class="cursor-pointer text-sm font-semibold text-white">Investigations and cleanup · {{ runnerCards.length }}</summary>
          <div id="stage-recovery" class="scroll-mt-32">
            <p class="mt-0.5 text-xs text-muted">Internal recovery is shown separately from productive workers.</p>
            <div v-if="runnerCards.length" class="mt-3 grid gap-2.5 sm:grid-cols-2 xl:grid-cols-3">
              <IssueCard v-for="card in runnerCards" :key="card.id" :card="card">
                <template #actions>
                  <UButton v-if="card.jobId" size="xs" color="error" variant="ghost" icon="i-lucide-rotate-ccw" aria-label="Reset" title="Reset" @click="ask(RESET, card.jobId)" />
                </template>
              </IssueCard>
            </div>
            <p v-else class="mt-2 text-xs text-muted">No internal work is running.</p>
          </div>
        </details>
      </section>

      <section id="completed" class="mt-4 scroll-mt-32 rounded-xl border border-line bg-bg p-3">
        <details :open="completedOpen" @toggle="event => completedOpen = (event.target as HTMLDetailsElement).open">
          <summary class="cursor-pointer text-sm font-semibold text-white">Completed issues · {{ groups.completed.length }}</summary>
          <div v-if="groups.completed.length" class="mt-3 grid gap-2.5 sm:grid-cols-2 xl:grid-cols-3">
            <IssueCard v-for="card in groups.completed" :key="card.key" :card="card" />
          </div>
          <p v-else class="mt-2 text-xs text-muted">No completed issues yet.</p>
        </details>
      </section>
    </template>

    <p class="sr-only" role="status" aria-live="polite">{{ announcement }}</p>

    <ConfirmDialog
      v-if="dialog"
      :open="!!dialog"
      :title="dialog.title"
      :text="dialog.text"
      :confirm-label="dialog.confirmLabel"
      :error="dialogError"
      @confirm="confirm"
      @close="closeDialog"
    />
  </div>
</template>
