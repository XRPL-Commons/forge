<script setup lang="ts">
import {
  COLUMN_ROLES, COLUMN_TITLES, destination, elapsed, factoryFreshness, groupIssues, isFresh, isProgressStatus, readout, warnings,
  type CardModel, type ColumnRole, type FactoryView, type Warning,
} from '~/utils/factory'
import { lineAnalyse, lineState } from '~/utils/factoryLine'
import { completedPath, mergeCompleted, modelCardView } from '~/utils/issueCard'
import type { NeedsFilter } from '~/utils/needsYou'
import { boardLink, panelQuery, panelState, panelTarget } from '~/utils/panel'

useHead({ title: 'Activity' })

const api = useForgeApi()
const route = useRoute()
const router = useRouter()

// A {ok:false, reason} answer is a state, not a failed request: it replaces
// the payload, so the page shows the fence instead of a stale factory.
const poll = useFactory()
const transition = useFactoryStore().transition

const now = useClock()
onMounted(poll.start)

const fence = computed(() => { const payload = poll.data.value; return payload && payload.ok === false ? payload : null })
const view = computed(() => { const payload = poll.data.value; return payload && payload.ok !== false ? payload : null })
const connectionLost = computed(() => !!poll.error.value && !!poll.data.value)
const fresh = computed(() => isFresh(view.value, now.value))

const freshness = computed(() => factoryFreshness(view.value, now.value, connectionLost.value))

// At most one message block above the factory line: connection lost -> the
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

const groups = computed(() => groupIssues(view.value, now.value))

// The one-line readout under the strip. "Needs you" is the strip's own crate
// count, so the two never disagree on the page.
const counts = computed(() => readout(view.value, now.value))
const needsYou = computed(() => {
  const state = lineState(view.value, now.value, groups.value)
  return state ? lineAnalyse(state).attention : 0
})

// Investigations and cleanup runs are drawn nowhere else, so they keep one
// collapsed line here; their cards load only when it is opened.
const runnersOpen = ref(false)
const runnersPoll = useFactory('/api/factory?section=runners', { allowed: () => runnersOpen.value, server: false })
onMounted(runnersPoll.start)
const runnerGroups = computed(() => groupIssues(runnersPoll.data.value?.ok === false ? null : runnersPoll.data.value, now.value))
const runnerCards = computed(() => [...runnerGroups.value.investigating, ...runnerGroups.value.internalJobs])
const runnerCount = computed(() => view.value?.summary?.runner_count ?? groups.value.investigating.length + groups.value.internalJobs.length)

// Recently completed: ten at a time, newest first, the next ten when the
// sentinel under the list scrolls into view (or from the Load more button).
// `completedNext` is undefined before the first page, null after the last.
const completed = ref<CardModel[]>([])
const completedNext = ref<string | null | undefined>(undefined)
const completedLoading = ref(false)
const completedError = ref('')
const completedSentinel = ref<HTMLElement | null>(null)
const completedCount = computed(() => view.value?.summary?.completed_count ?? groups.value.completed.length)

async function completedPage(before: string | null) {
  const page = await api.get<FactoryView & { next?: string | null }>(completedPath(before))
  return { cards: groupIssues(page, now.value).completed, next: page.next ?? null }
}

function sentinelNear(): boolean {
  const el = completedSentinel.value
  return !!el && el.getBoundingClientRect().top < window.innerHeight + 200
}

async function loadCompleted() {
  if (completedLoading.value || completedNext.value === null) return
  completedLoading.value = true
  completedError.value = ''
  let loaded = false
  try {
    const { cards, next } = await completedPage(completedNext.value ?? null)
    completed.value = mergeCompleted(completed.value, cards)
    completedNext.value = next
    loaded = true
  } catch (error: unknown) {
    completedError.value = error instanceof Error ? error.message : 'Completed issues are unavailable.'
  } finally {
    completedLoading.value = false
  }
  // A tall screen can still show the sentinel after a page: keep filling it.
  await nextTick()
  if (loaded && sentinelNear()) void loadCompleted()
}

// An issue that completes while the page is open joins the top of the list.
watch(completedCount, async (count, before) => {
  if (!import.meta.client || before === undefined || count <= before || !completed.value.length) return
  try { completed.value = mergeCompleted(completed.value, (await completedPage(null)).cards) } catch { /* the next change retries */ }
})

let sentinelObserver: IntersectionObserver | null = null
watch(completedSentinel, (el, old) => {
  if (old) sentinelObserver?.unobserve(old)
  if (!el || !import.meta.client) return
  sentinelObserver ??= new IntersectionObserver(entries => { if (entries.some(entry => entry.isIntersecting)) void loadCompleted() }, { rootMargin: '0px 0px 200px 0px' })
  sentinelObserver.observe(el)
}, { flush: 'post' })
onBeforeUnmount(() => { sentinelObserver?.disconnect(); sentinelObserver = null })

// Hash targets can arrive before the first factory snapshot, or point inside
// the collapsed investigations line. Open it before scrolling to the target.
async function revealSection() {
  if (!view.value) return
  const hash = route.hash
  if (hash === '#stage-recovery' || hash === '#runners') runnersOpen.value = true
  else if (hash === '#needs-attention') { showAttention('all'); return }
  else if (hash !== '#completed' && hash !== '#factory-line' && hash !== '#pull-request-work') return
  await nextTick()
  document.getElementById(hash.slice(1))?.scrollIntoView({ block: 'start' })
}
watch([() => route.hash, () => !!view.value], () => { if (import.meta.client) void revealSection() }, { flush: 'post' })
onMounted(revealSection)

// The strip draws the stages; a screen reader still hears when an issue
// moves from one to the next.
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

// The page decides nothing itself: a card opens the issue's panel, and every
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
// The issues that need the operator live in the factory line's crate; the
// crate, its bays and the "Needs you" count open them as a list.
const needsOpen = ref(false)
const needsFilter = ref<NeedsFilter>('all')
function showAttention(filter: NeedsFilter = 'all') {
  needsFilter.value = filter
  needsOpen.value = true
}
function close() {
  void router.replace({ query: panelQuery(route.query, null) })
}

// Closing the panel hands focus back to the card that opened it, when that
// card is still on the page (a card opened from inside a stack is not).
watch(() => panel.value.key, (key, before) => {
  // An issue opened from the list (its card or its action) replaces the list.
  if (key) needsOpen.value = false
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
        :to="boardLink(operator!.action_url)"
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
      <FactoryLine id="factory-line" class="mt-5 scroll-mt-32" :view="view" :now="now" :groups="groups" :transition="transition" @open="open" @attention="showAttention" />

      <p v-if="counts" id="readout" class="readout mt-3 flex flex-wrap gap-x-5 gap-y-1 border-y border-line py-2 text-xs text-muted tabular-nums sm:justify-end">
        <span v-if="view.pull_requests?.length" data-readout="working">Workers running <b class="font-semibold text-white">{{ counts.working }}</b> of {{ counts.slots }}<span v-if="counts.occupied > counts.working"> · {{ counts.occupied }} slots in use</span></span>
        <span v-else data-readout="working">Working <NuxtLink to="/issues?show=working" class="font-semibold text-white hover:underline" :aria-label="`${counts.working} of ${counts.slots} slots working`">{{ counts.working }}</NuxtLink> of {{ counts.slots }}<span v-if="counts.occupied > counts.working" class="text-muted"> · {{ counts.occupied }} {{ counts.occupied === 1 ? 'slot' : 'slots' }} in use</span></span>
        <span data-readout="queued">{{ view.pull_requests?.length ? 'Issues queued' : 'Queued' }} <NuxtLink to="/issues?show=queued" class="font-semibold text-white hover:underline" :aria-label="`${counts.queued} queued`">{{ counts.queued }}</NuxtLink></span>
        <span data-readout="merging">{{ view.pull_requests?.length ? 'Issues waiting to merge' : 'Waiting to merge' }} <b class="font-semibold text-white">{{ counts.merging }}</b></span>
        <span data-readout="attention">{{ view.pull_requests?.length ? 'Issues needing you' : 'Needs you' }} <button type="button" class="cursor-pointer font-semibold hover:underline focus-visible:outline-2 focus-visible:outline-sky-200" :class="needsYou ? 'text-amber-200' : 'text-white'" aria-haspopup="dialog" :aria-label="`Show the ${needsYou} issue${needsYou === 1 ? '' : 's'} that need${needsYou === 1 ? 's' : ''} you`" @click="showAttention('all')">{{ needsYou }}</button></span>
        <span data-readout="merged">Merged today <a href="#completed" class="font-semibold text-white hover:underline" :aria-label="`${counts.merged} merged today, go to Recently completed`">{{ counts.merged }}</a></span>
      </p>

      <PullRequestWorkList :items="view.pull_requests ?? []" :more="view.pull_requests_next" :recent-truncated="view.pull_requests_recent_truncated" class="mt-6" />

      <!-- Investigations and cleanup runs are drawn nowhere else: one line,
           opened on demand, so an internal run can still be reset. -->
      <section id="runners" class="mt-6 scroll-mt-32">
        <details :open="runnersOpen" @toggle="event => runnersOpen = (event.target as HTMLDetailsElement).open">
          <summary class="cursor-pointer text-sm text-muted hover:text-white">Investigations and cleanup · {{ runnerCount }}</summary>
          <div v-if="runnersOpen" id="stage-recovery" class="scroll-mt-32">
            <p v-if="runnersPoll.error.value" role="status" class="mt-2 text-xs text-amber-200">{{ runnersPoll.error.value }}</p>
            <p v-else-if="!runnersPoll.data.value" class="mt-2 text-xs text-muted">Loading investigations…</p>
            <div v-if="runnerCards.length" class="mt-3 grid gap-2.5 md:grid-cols-2 xl:grid-cols-3">
              <IssueCard v-for="card in runnerCards" :key="card.id" :view="modelCardView(card, now)" compact @open="open(card)" />
            </div>
            <p v-else-if="runnersPoll.data.value" class="mt-2 text-xs text-muted">No internal work is running.</p>
          </div>
        </details>
      </section>

      <section id="completed" class="mt-6 scroll-mt-32" aria-labelledby="completed-title">
        <h2 id="completed-title" class="text-sm font-semibold text-white">Recently completed · {{ completedCount }}</h2>
        <div v-if="completed.length" class="mt-3 grid gap-2.5 md:grid-cols-2 xl:grid-cols-3">
          <IssueCard v-for="card in completed" :key="card.key" :view="modelCardView(card, now)" compact @open="open(card)" />
        </div>
        <p v-if="completedError" role="status" class="mt-2 text-xs text-amber-200">{{ completedError }}</p>
        <p v-if="completedLoading" class="mt-2 text-xs text-muted">Loading completed issues…</p>
        <p v-else-if="completedNext === null && !completed.length" class="mt-2 text-xs text-muted">No completed issues yet.</p>
        <p v-else-if="completedNext === null" id="completed-end" class="mt-3 text-center text-xs text-muted">That's everything.</p>
        <div v-if="completedNext !== null" ref="completedSentinel" class="mt-3 flex justify-center" data-completed-sentinel>
          <UButton v-if="!completedLoading && (completedNext || completedError)" size="xs" color="neutral" variant="outline" @click="loadCompleted">{{ completedError ? 'Try again' : 'Load more' }}</UButton>
        </div>
      </section>
    </template>

    <p class="sr-only" role="status" aria-live="polite">{{ announcement }}</p>

    <NeedsYouModal v-if="view" v-model:open="needsOpen" v-model:filter="needsFilter" :view="view" :now="now" :groups="groups" @open="open" />

    <!-- The panel is an overlay teleported to the body: it belongs to the
         browser, not to the first paint. -->
    <ClientOnly>
      <IssuePanel v-if="panel.key" v-model:open="panelOpen" :issue-key="panel.key" />
    </ClientOnly>
  </div>
</template>
