<script setup lang="ts">
import { ForgeApiError } from '~/composables/useForgeApi'
import {
  COLUMN_ROLES, COLUMN_TITLES, destination, elapsed, groupIssues, isFresh, breakdown, isProgressStatus, warnings,
  type CardModel, type ColumnRole, type FactoryView, type Warning,
} from '~/utils/factory'
import { panelQuery, panelState, panelTarget } from '~/utils/panel'

interface Fence { ok: false; reason: string }

useHead({ title: 'Activity' })

const api = useForgeApi()
const route = useRoute()
const router = useRouter()

// A {ok:false, reason} answer is a state, not a failed request: it replaces
// the payload, so the page shows the fence instead of a stale factory.
const poll = useFactory()

const now = useClock()
onMounted(poll.start)

const fence = computed(() => { const payload = poll.data.value; return payload && payload.ok === false ? payload : null })
const view = computed(() => { const payload = poll.data.value; return payload && payload.ok !== false ? payload : null })
const connectionLost = computed(() => !!poll.error.value && !!poll.data.value)
const fresh = computed(() => isFresh(view.value, now.value))
const observedAt = computed(() => (view.value?.observation as { at?: number } | undefined)?.at ?? null)

const freshness = computed(() => {
  if (connectionLost.value) return 'Reconnecting · last known state'
  if (!view.value) return 'Connecting…'
  if (fresh.value && observedAt.value) return `Live · checked ${elapsed(now.value - observedAt.value)} ago`
  return 'Updates delayed · last known state'
})

// At most one message block above the metrics: connection lost -> the
// freshness pill only; otherwise the banner(s); otherwise the operator
// status line. A {ok:false, reason} payload leaves no state to show, so its
// reason is the page's single banner.
const banners = computed<Warning[]>(() => {
  if (connectionLost.value) return []
  if (fence.value) return [{ kind: 'other', text: fence.value.reason, ...destination('other', route.path) }]
  if (!view.value) {
    const reason = poll.error.value
    return reason ? [{ kind: 'other', text: reason, ...destination('other', route.path) }] : []
  }
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
const showStatusLine = computed(() => !!view.value && !connectionLost.value && !banners.value.length && !!operator.value?.title && !isProgressStatus(operator.value.title))

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
  return [`${working} working`, `${waiting} queued`, ...(between ? [`${between} ${fresh.value && !poll.error.value ? 'starting / finishing' : 'last observed'}`] : []), `${slots.value[role] ?? 0} maximum`].join(' · ')
}

const attentionCount = computed(() => groups.value.attention.length + groups.value.held.length)

const runnerCards = computed(() => [...runnerGroups.value.investigating, ...runnerGroups.value.internalJobs])
// Expanded whenever the section holds a job the supervisor last reported as
// running, so an active investigation is never hidden behind a disclosure.
const runnersAuto = computed(() => view.value?.summary?.runners_running ?? [...groups.value.investigating, ...groups.value.internalJobs].some(card => card.running || card.phaseLabel === 'Last seen working'))
const runnersManual = ref<boolean | null>(null)
const runnersOpen = computed(() => runnersManual.value ?? runnersAuto.value)
const completedOpen = useState('activity-completed-open', () => false)

const completedPoll = useFactory('/api/factory?section=completed', { allowed: () => completedOpen.value, server: false })
const runnersPoll = useFactory('/api/factory?section=runners', { allowed: () => runnersOpen.value, server: false })
onMounted(() => { completedPoll.start(); runnersPoll.start() })
const completedGroups = computed(() => groupIssues(completedPoll.data.value?.ok === false ? null : completedPoll.data.value, now.value))
const runnerGroups = computed(() => groupIssues(runnersPoll.data.value?.ok === false ? null : runnersPoll.data.value, now.value))
const completedCount = computed(() => view.value?.summary?.completed_count ?? groups.value.completed.length)
const runnerCount = computed(() => view.value?.summary?.runner_count ?? groups.value.investigating.length + groups.value.internalJobs.length)
watch(() => route.hash, hash => {
  if (hash === '#completed') completedOpen.value = true
  if (hash === '#runners' || hash === '#stage-recovery') runnersManual.value = true
}, { immediate: true })
// Hash targets can arrive before the first factory snapshot, or point inside
// a manually collapsed disclosure. Open it before scrolling to the target.
async function revealSection() {
  if (!view.value) return
  const hash = route.hash
  if (hash === '#stage-recovery' || hash === '#runners') runnersManual.value = true
  else if (hash === '#completed') completedOpen.value = true
  else return
  await nextTick()
  document.getElementById(hash.slice(1))?.scrollIntoView({ block: 'start' })
}
watch([() => route.hash, () => !!view.value], () => { if (import.meta.client) void revealSection() }, { flush: 'post' })
onMounted(revealSection)


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

// The board decides nothing itself: a card opens the issue's panel, and every
// action — decisions, Stop, Reset — is taken there. The open panel lives in
// the URL (`?issue=<key>`, plus `?job=<id>` for an internal run), so a reload
// and the back button restore exactly what the operator was looking at.
const panel = computed(() => panelState(route.query))
const panelOpen = computed({
  get: () => !!panel.value.key,
  set: (value: boolean) => { if (!value) close() },
})

function open(card: CardModel) {
  void router.replace({ query: panelQuery(route.query, panelTarget(card)) })
}
function close() {
  void router.replace({ query: panelQuery(route.query, null) })
}

// Closing the panel hands focus back to the card that opened it, when that
// card is still on the board (a card opened from inside a stack is not).
watch(() => panel.value.key, (key, before) => {
  if (key || !before) return
  const opener = before
  nextTick(() => requestAnimationFrame(() => {
    document.querySelector<HTMLElement>(`.issue-card[data-key="${CSS.escape(opener)}"]`)?.focus()
  }))
})
</script>

<template>
  <div>
    <div class="flex flex-wrap items-end justify-between gap-3">
      <div>
        <p class="text-xs uppercase tracking-wide text-muted">Live operations</p>
        <h1 class="text-3xl font-semibold text-white">Activity</h1>
      </div>
      <p
        v-if="!fence || connectionLost"
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
          @open="open"
        />
      </section>

      <!-- Issues waiting on a decision stay expanded; a deliberate hold waits
           as one stack, although the header counts both. -->
      <section id="needs-attention" class="mt-6 scroll-mt-32 rounded-xl border border-line bg-bg p-3">
        <h2 class="text-sm font-semibold text-white">Needs attention · {{ attentionCount }}</h2>
        <div v-if="groups.attention.length" class="mt-3 grid gap-2.5 sm:grid-cols-2 xl:grid-cols-3">
          <IssueCard v-for="card in groups.attention" :key="card.key" :card="card" @open="open(card)" />
        </div>
        <IssueStack v-if="groups.held.length" class="mt-3" :cards="groups.held" noun="on hold" @open="open" />
        <p v-if="!attentionCount" class="mt-2 text-xs text-muted">No issues need your attention.</p>
      </section>

      <!-- Blocked by open dependencies: not queued for a worker, and back in
           its stage by itself once the issues it names are closed. -->
      <section v-if="groups.waiting.length" id="waiting" class="mt-4 scroll-mt-32 rounded-xl border border-line bg-bg p-3">
        <h2 class="text-sm font-semibold text-white">Waiting on other issues · {{ groups.waiting.length }}</h2>
        <p class="mt-0.5 text-xs text-muted">These start by themselves once the issues they depend on are closed.</p>
        <div class="mt-3 grid gap-2.5 sm:grid-cols-2 xl:grid-cols-3">
          <IssueCard v-for="card in groups.waiting" :key="card.key" :card="card" @open="open(card)" />
        </div>
      </section>

      <section id="finishing" class="mt-4 scroll-mt-32 rounded-xl border border-line bg-bg p-3">
        <h2 class="text-sm font-semibold text-white">Finishing</h2>
        <p class="mt-0.5 text-xs text-muted">Approved work waiting to merge, or being merged.</p>
        <div v-if="groups.finishing.length" class="mt-3 grid gap-2.5 sm:grid-cols-2 xl:grid-cols-3">
          <IssueCard v-for="card in groups.finishing" :key="card.key" :card="card" @open="open(card)" />
        </div>
        <p v-else class="mt-2 text-xs text-muted">No issues waiting to merge.</p>
      </section>

      <section id="runners" class="mt-4 scroll-mt-32 rounded-xl border border-line bg-bg p-3">
        <details :open="runnersOpen" @toggle="event => runnersManual = (event.target as HTMLDetailsElement).open">
          <summary class="cursor-pointer text-sm font-semibold text-white">Investigations and cleanup · {{ runnerCount }}</summary>
          <div v-if="runnersOpen" id="stage-recovery" class="scroll-mt-32">
            <p class="mt-0.5 text-xs text-muted">Internal recovery is shown separately from productive workers.</p>
            <p v-if="runnersPoll.error.value" role="status" class="mt-2 text-xs text-amber-200">{{ runnersPoll.error.value }}</p>
            <p v-else-if="!runnersPoll.data.value" class="mt-2 text-xs text-muted">Loading investigations…</p>
            <div v-if="runnerCards.length" class="mt-3 grid gap-2.5 sm:grid-cols-2 xl:grid-cols-3">
              <IssueCard v-for="card in runnerCards" :key="card.id" :card="card" @open="open(card)" />
            </div>
            <p v-else-if="runnersPoll.data.value" class="mt-2 text-xs text-muted">No internal work is running.</p>
          </div>
        </details>
      </section>

      <section id="completed" class="mt-4 scroll-mt-32 rounded-xl border border-line bg-bg p-3">
        <details :open="completedOpen" @toggle="event => completedOpen = (event.target as HTMLDetailsElement).open">
          <summary class="cursor-pointer text-sm font-semibold text-white">Completed issues · {{ completedCount }}</summary>
          <p v-if="completedOpen && completedPoll.error.value" role="status" class="mt-2 text-xs text-amber-200">{{ completedPoll.error.value }}</p>
          <p v-else-if="completedOpen && !completedPoll.data.value" class="mt-2 text-xs text-muted">Loading completed issues…</p>
          <div v-if="completedOpen && completedGroups.completed.length" class="mt-3 grid gap-2.5 sm:grid-cols-2 xl:grid-cols-3">
            <IssueCard v-for="card in completedGroups.completed" :key="card.key" :card="card" @open="open(card)" />
          </div>
          <p v-else-if="completedOpen && completedPoll.data.value" class="mt-2 text-xs text-muted">No completed issues yet.</p>
        </details>
      </section>
    </template>

    <p class="sr-only" role="status" aria-live="polite">{{ announcement }}</p>

    <!-- The panel is an overlay teleported to the body: it belongs to the
         browser, not to the first paint. -->
    <ClientOnly>
      <IssuePanel v-if="panel.key" v-model:open="panelOpen" :issue-key="panel.key" />
    </ClientOnly>
  </div>
</template>
