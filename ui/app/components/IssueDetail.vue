<script setup lang="ts">
import { decisionFeedback, receiptAction, type DecisionReceipt } from '~/utils/decision-feedback'
import { ForgeApiError } from '~/composables/useForgeApi'
import { deploymentLine, type FactoryView, type JobView } from '~/utils/factory'
import { issueTab, tabQuery, type IssueTab } from '~/utils/discussion'
import { statusBadge } from '~/utils/tones'
import { issuePipeline, type PipelineView } from '~/utils/pipeline'
import { workbenchLook } from '~/utils/workbench'
import { panelQuery } from '~/utils/panel'
import {
  ACTION_LABELS, approvalLines, attentionNote, blockingIssues, statusLabel, checkedText, eventLabel, heldByLines, issuePath, lastAction, pendingAction, preselectedReviewers,
  pullLabel, reviewPull, reviewRequestText, reviewRows, reviewerReason, summaryBlock,
  type IssueAction, type IssueDetail as IssueDetailView, type IssueDetailResponse,
  type IssueEnvironmentSelection,
} from '~/utils/issues'

// The one detail view: /issues/<key> owns the issue's decisions and its
// worker output. It polls the issue itself and the factory (for the issue's
// jobs, which the issue payload does not carry), and it owns both POSTs —
// DecisionPanel only reports what the operator confirmed.
// `fill` says what the Discussion tab's chat fills: the drawer's body
// ('container') or the rest of the window below the page header ('viewport').
const props = withDefaults(defineProps<{ issueKey: string; fill?: 'container' | 'viewport' }>(), { fill: 'viewport' })

const UNMANAGED = 'This issue is not in a managed repository. Refresh the issue list.'
// The way forward, in the order the issue list names it (rowPrimary).
const PRIMARY_ACTIONS: IssueAction[] = ['assign_reviewer', 'retry', 'recheck', 'resume', 'queue']

const api = useForgeApi()
const route = useRoute()

interface Loaded { detail: IssueDetailResponse | null; fence: string | null; factory: FactoryView | null }

// True while a decision dialog is open or a POST is in flight, so a poll can
// never re-render the issue under the operator's hands.
const submitting = ref(false)
const environmentOpen = ref(false)
// Each request the page sends has one submission: its spinner, its double
// submit guard and the error its dialog shows.
const environmentRequest = useSubmission('Could not save the environment.')
const verificationRequest = useSubmission('Could not confirm verification.')
const verificationStatement = ref('')
const verificationIdentity = ref<{ gate: string; revision: string; statement: string; id: string } | null>(null)

const factory = useFactory(() => '/api/factory?issue=' + encodeURIComponent(props.issueKey))
const poll = useResource<Omit<Loaded, 'factory'>>(() => `/api/issues?issue=${encodeURIComponent(props.issueKey)}`, async (path) => {
  try { return { detail: await api.get<IssueDetailResponse>(path, { timeout: 30000 }), fence: null } }
  catch (error) {
    if (error instanceof ForgeApiError && error.status === 200) return { detail: null, fence: error.message }
    if (error instanceof ForgeApiError && error.status === 404) return { detail: null, fence: UNMANAGED }
    throw error
  }
}, { allowed: () => !submitting.value && !environmentOpen.value })
// Warm detail opens synchronously; cold direct links retain SSR data.
if (import.meta.server) await poll.refresh(5000)
const mounted = ref(false)
onMounted(() => { mounted.value = true; poll.start(); factory.start() })
const loaded = computed<Loaded | null>(() => {
  const value = poll.data.value
  return value ? { ...value, factory: factory.data.value?.ok === false ? null : factory.data.value } : null
})
const detail = computed<IssueDetailView | null>(() => loaded.value?.detail?.issue ?? null)
watch(() => detail.value?.verification_request?.gate_id, (gate, previous) => {
  if (previous && gate !== previous) {
    verificationStatement.value = ''
    verificationIdentity.value = null
    verificationRequest.error.value = gate ? 'The verification request changed. Check the current candidate before confirming.' : null
  }
})
const actions = computed(() => loaded.value?.detail?.actions ?? [])
const fence = computed(() => loaded.value?.fence ?? null)

// Nothing to show yet: either the fence/404 copy, or the failure of a first
// load that never returned an issue to fall back on.
const problem = computed(() => fence.value ?? (detail.value ? null : poll.error.value))

const pending = computed(() => (detail.value ? pendingAction(actions.value, detail.value.key) : null))
const busy = computed(() => submitting.value || !!pending.value)

// Description: rendered and sanitized server-side, re-rendered whenever the
// body changes (an operator edit on GitHub arrives through the poll). Lazy:
// server rendering still includes it, but client navigation never waits for
// the render round trip before painting the (cached) issue.
const { data: html } = await useAsyncData(`md-${props.issueKey}`, async () => {
  const issue = detail.value
  if (!issue?.body) return ''
  const rendered = await $fetch<{ html: string }>('/_ui/markdown', { method: 'POST', body: { text: issue.body, repo: issue.repo } })
  return rendered.html
}, { watch: [() => detail.value?.body], lazy: true })

const submitted = ref<DecisionReceipt | null>(null)
const workerReceipt = ref<{ id: string; before: string } | null>(null)
const decisionRequest = useSubmission()
const decision = ref<IssueAction | null>(null)

// One message line, in the order the operator needs it: what Forge is doing
// with the last decision, then whether what they are looking at is current,
// then the receipt for what they just submitted.
const observedDecision = computed(() => workerReceipt.value ? null : receiptAction(actions.value, props.issueKey, submitted.value))
const message = computed(() => decisionFeedback(observedDecision.value, submitted.value, poll.stale.value))
watch([observedDecision, submitted], ([action]) => {
  if (submitted.value && action && action.status !== 'pending') submitted.value = null
})
watch(() => props.issueKey, () => { submitted.value = null; workerReceipt.value = null })

function previousDecision(): string | null {
  return lastAction(actions.value, props.issueKey)?.id ?? null
}
function recordDecision(reply: { request?: string }, previous: string | null, message?: string) {
  workerReceipt.value = null
  submitted.value = { request: reply.request ?? null, previous, message }
}


// What the page leads with: the issue's cube on its workbench, saying Forge's
// summary — one headline sentence, at most three plain sentences — then the
// one way forward. Forge's status line and the worker's prose wait in Review
// findings, so nobody has to open it to learn where this issue stands or why.
const summary = computed(() => summaryBlock(detail.value))
const pipeline = computed(() => (detail.value ? issuePipeline(detail.value) : null))
// The workbench: the issue's cube, its mood and props, the stages it earned
// and the attempts it has left — all from the same payload.
const look = computed(() => (detail.value ? workbenchLook(detail.value) : null))
const badge = computed(() => statusBadge(detail.value?.status ?? 'queued'))
// The open prerequisites a blocked issue waits on: the action area names each
// one, and each opens that issue.
const blocking = computed(() => (detail.value ? blockingIssues(detail.value) : []))
// Another issue of the same repository: in the board's panel it opens in the
// panel (and the back button returns here); on the issue route, as a page.
function siblingLink(number: number) {
  const key = `${detail.value?.repo_id}:${number}`
  return route.path === '/' ? { path: '/', query: panelQuery(route.query, { key }) } : issuePath(key)
}
const NOTE_TONE: Record<PipelineView['state'], string> = {
  done: 'text-mint-200', active: 'text-sky-200', waiting: 'text-sky-200', failed: 'text-rose-200',
  blocked: 'text-amber-200', upcoming: 'text-muted', closed: 'text-muted',
}
const detailsOpen = useState(`issue-details-${props.issueKey}`, () => false)
const descriptionOpen = ref(false)
const environmentSectionOpen = ref(false)
const conversation = ref<{ focus: () => boolean } | null>(null)
const plainOpen = ref(false)
const PLAIN_CLAMP = 220
const plainLong = computed(() => (summary.value?.plain ?? '').length > PLAIN_CLAMP)
const holds = computed(() => heldByLines(detail.value?.held_by))
// The worker environment is always one of the collapsed sections: when it is
// what stopped the issue, the summary above already says so.
const hasFindings = computed(() => !!summary.value?.details || reviews.value.length > 0)

const issueUrl = computed(() => (detail.value ? `https://github.com/${detail.value.repo}/issues/${detail.value.number}` : ''))
function pullUrl(number: number) { return `https://github.com/${detail.value?.repo}/pull/${number}` }

// Assigning a human reviewer is the one decision with an argument: its
// button opens a dialog where the operator picks from the people Forge found
// for this repository (never a typed login), starting from the configured
// human reviewers.
const canAssign = computed(() => !!detail.value?.actions.includes('assign_reviewer'))
const decisions = computed(() => (detail.value?.actions ?? []).filter(action => action !== 'assign_reviewer'))
// One primary button: approving when Forge waits for it, else the decision
// that moves the issue on, else repository setup when that is the blocker.
const setupNeeded = computed(() => !!(detail.value?.setup_needed || detail.value?.merge_blocker))
const primary = computed<'approve' | 'setup' | IssueAction | null>(() => {
  if (approval.value) return 'approve'
  const action = PRIMARY_ACTIONS.find(item => (detail.value?.actions ?? []).includes(item as never))
  if (action) return action
  return setupNeeded.value ? 'setup' : null
})
const secondary = computed(() => decisions.value.filter(action => action !== primary.value))
const closeOpen = computed(() => decision.value === 'close')
const assigning = ref(false)
const assignRequest = useSubmission()
const preselected = computed(() => preselectedReviewers(detail.value))
const candidatePull = computed(() => reviewPull(detail.value?.pull_requests))
const approvalOpen = ref(false)
// Set once the operator went to GitHub from the dialog: only then is
// "waiting for your approval" news rather than a repeat of the headline.
const sentToGitHub = ref(false)
const approval = computed(() => detail.value?.approval ?? null)
// The card opens this same dialog; no extra detail fetch or independent cache.
watch([approval, () => route.query.approve], ([value, asked]) => {
  if (value && asked === '1') {
    approvalOpen.value = true
    void navigateTo({ path: route.path, query: { ...route.query, approve: undefined } }, { replace: true })
  }
  if (!value) approvalOpen.value = false
}, { immediate: true })
const candidateUrl = computed(() => (candidatePull.value ? pullUrl(candidatePull.value.number) : issueUrl.value))

function askReviewer() {
  approvalOpen.value = false
  assignRequest.reset()
  assigning.value = true
  submitting.value = true
}
function closeReviewer() {
  if (assignRequest.pending.value) return
  assigning.value = false
  assignRequest.reset()
  submitting.value = false
}

// "· 12 min ago" needs a clock the server does not have, so the age only
// appears once mounted; the sentence itself renders either way.
const now = ref(0)
let ticker: ReturnType<typeof setInterval> | undefined
onMounted(() => {
  now.value = Math.floor(Date.now() / 1000)
  ticker = setInterval(() => { now.value = Math.floor(Date.now() / 1000) }, 5000)
})
onBeforeUnmount(() => clearInterval(ticker))
const reviewRequested = computed(() => reviewRequestText(detail.value?.reviewers_requested, now.value))

// The reviews Forge found on GitHub and when it last looked, so an approval
// that did not count says why instead of being silently ignored.
function shortTime(at: number): string {
  const date = new Date(at * 1000)
  const today = new Date().toDateString() === date.toDateString()
  return date.toLocaleString([], today ? { hour: '2-digit', minute: '2-digit' } : { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}
const approvals = computed(() => approvalLines(detail.value?.approvals, mounted.value ? shortTime : undefined))
const checked = computed(() => checkedText(detail.value?.last_checked, now.value))

// "Check now" asks the supervisor to read this issue first next cycle, then
// polls the detail until `last_checked` moves past the click.
const CHECK_POLL_MS = 4000
const CHECK_GIVE_UP_S = 90
const checkingSince = ref<number | null>(null)
const checkNote = ref('')
const checkRequest = useSubmission('Forge did not accept the request.')
let checkTimer: ReturnType<typeof setInterval> | undefined
function stopChecking(note = '') {
  clearInterval(checkTimer)
  checkingSince.value = null
  checkNote.value = note
}
async function checkNow() {
  const issue = detail.value
  if (!issue || checkingSince.value || checkRequest.pending.value) return
  checkNote.value = ''
  const since = Math.floor(Date.now() / 1000)
  if (!await checkRequest.run(() => api.act('/api/issues/refresh', { key: issue.key }))) {
    checkNote.value = checkRequest.error.value ?? ''
    return
  }
  checkingSince.value = since
  clearInterval(checkTimer)
  checkTimer = setInterval(() => {
    if (document.hidden) return
    if (Date.now() / 1000 - since > CHECK_GIVE_UP_S) stopChecking('Still checking. Forge reads GitHub every cycle, so this page will update on its own.')
    else void poll.refresh()
  }, CHECK_POLL_MS)
  void poll.refresh()
}
watch(() => detail.value?.last_checked, (value) => {
  if (checkingSince.value && value && value > checkingSince.value) stopChecking()
})
watch(() => props.issueKey, () => stopChecking())
onBeforeUnmount(() => clearInterval(checkTimer))

// Whether the merge reached production is the ledger's verdict, which travels
// on the factory view rather than on the issue payload — the same view this
// page already polls for the issue's jobs.
const deployment = computed(() => {
  const factory = loaded.value?.factory ?? null
  return deploymentLine((factory?.issues ?? []).find(issue => issue.key === props.issueKey), factory, now.value)
})

async function assignReviewer(logins: string[]) {
  const issue = detail.value
  const reason = reviewerReason(logins)
  if (!issue || !reason) return
  const done = await assignRequest.run(async () => {
    const previous = previousDecision()
    const reply = await api.act<{ request?: string }>('/api/issues/action', {
      key: issue.key, action: 'assign_reviewer', revision: issue.revision, reason,
    })
    recordDecision(reply, previous)
  })
  if (!done) return
  closeReviewer()
  await poll.refresh()
}

// Reviews are compact rows — the verdict, the role, the lead sentence — inside
// Details; "More" reveals the worker's full text. The chip is that reviewer's
// verdict, never the issue's status, which the badge in the title line carries.
const reviews = computed(() => reviewRows(detail.value?.reviews))
const expanded = ref(new Set<string>())
function toggleReview(role: string) {
  const next = new Set(expanded.value)
  if (!next.delete(role)) next.add(role)
  expanded.value = next
}

// Times are formatted only after mount: the server has no idea which time
// zone the operator is in, and a hydration mismatch is not worth a second.
function localTime(at: number): string {
  const iso = new Date(at * 1000).toISOString()
  return mounted.value ? new Date(at * 1000).toLocaleString() : iso
}
function isoTime(at: number): string { return new Date(at * 1000).toISOString() }

// Every job this issue has had, newest first — a finished failed attempt and
// a recovery investigation stay reachable, not only the running one.
// Running jobs plus the issue's recent finished attempts, whose output stays readable.
const jobs = computed<JobView[]>(() => {
  const live = (loaded.value?.factory?.jobs ?? []).filter(job => job.issue_key === props.issueKey)
  const row = (loaded.value?.factory?.issues ?? []).find(issue => issue.key === props.issueKey)
  const past = (row?.attempts ?? [])
    .filter(job => !live.some(running => running.id === job.id))
    .map(job => ({ ...job, issue_key: props.issueKey }) as JobView)
  return [...live, ...past]
    .sort((left, right) => (right.started_at ?? right.created_at ?? 0) - (left.started_at ?? left.created_at ?? 0))
})

// `?job=<id>` is a link to one run — what the operator follows out of the
// activity list or a shared message.
const requestedJob = computed<string | null>(() => {
  const asked = route.query.job
  const job = Array.isArray(asked) ? asked[0] : asked
  return typeof job === 'string' && job ? job : null
})
const selectedJob = computed<string | null>(() => requestedJob.value ?? detail.value?.active ?? jobs.value[0]?.id ?? null)

// That run's output stays reachable even when the issue behind it is not: a
// repository that left the inventory (or a reset fence) fails the detail
// request, and the log the operator came to read would go with it. The
// alert above still says why the rest of the page is missing.
const showOutput = computed(() => !!detail.value || !!requestedJob.value)

// Investigations and cleanup have no decisions of their own: their one
// control is Reset, which the board used to carry on the card. A job is
// internal exactly when no issue claims it as its active attempt — the same
// rule the board groups by — so an issue's own attempt is still stopped, not
// reset.
const internalJob = computed<JobView | null>(() => {
  const id = requestedJob.value
  if (!id) return null
  const factory = loaded.value?.factory
  const job = (factory?.jobs ?? []).find(row => row.id === id) ?? null
  if (!job) return null
  return (factory?.issues ?? []).some(issue => issue.active === id) ? null : job
})

function workerState(id: string): string {
  const job = jobs.value.find(job => job.id === id)
  return JSON.stringify(job ? [job.phase, job.outcome, job.cleanup] : null)
}
watch(() => workerReceipt.value && workerState(workerReceipt.value.id), value => {
  if (workerReceipt.value && value !== workerReceipt.value.before) {
    workerReceipt.value = null
    submitted.value = null
  }
})

const RESET = {
  title: 'Reset runner?',
  text: 'This terminates the old execution and discards its disposable workspace. Capacity is released only after teardown is verified.',
  confirmLabel: 'Reset runner',
}
const resetting = ref(false)
const resetRequest = useSubmission()

function askReset() {
  resetRequest.reset()
  resetting.value = true
  submitting.value = true
}
function closeReset() {
  if (resetRequest.pending.value) return
  resetting.value = false
  submitting.value = false
}
async function confirmReset(reason: string) {
  const job = internalJob.value
  if (!job) return
  const done = await resetRequest.run(async () => {
    const before = workerState(job.id)
    await api.act('/api/factory/action', { action: 'reset', target: job.id, reason })
    submitted.value = { request: null, previous: previousDecision() }
    workerReceipt.value = { id: job.id, before }
  })
  if (!done) return
  closeReset()
  await poll.refresh()
}

const outputOpen = ref(false)
const activityOpen = ref(false)

// `reason` prefills the dialog, e.g. with what a discussion proposed.
const decisionReason = ref('')
function ask(action: IssueAction, reason = '') {
  decisionRequest.reset()
  decisionReason.value = reason
  decision.value = action
  submitting.value = true
}

async function confirmVerification() {
  const issue = detail.value
  const gate = issue?.verification_request?.gate_id
  const statement = verificationStatement.value.trim()
  if (!issue || !gate || !statement || busy.value) return
  if (verificationIdentity.value?.gate !== gate || verificationIdentity.value?.revision !== issue.revision || verificationIdentity.value?.statement !== statement) {
    verificationIdentity.value = { gate, revision: issue.revision, statement, id: crypto.randomUUID() }
  }
  const requestId = verificationIdentity.value.id
  submitting.value = true
  const confirmed = await verificationRequest.run(() => api.act('/api/issues/verify', {
    key: issue.key, revision: issue.revision, gate_id: gate, statement, request_id: requestId,
  }))
  submitting.value = false
  if (confirmed) verificationStatement.value = ''
  await poll.refresh()
  if (confirmed) await factory.refresh()
}

async function saveEnvironment(environment: IssueEnvironmentSelection) {
  const issue = detail.value
  if (!issue || busy.value) return
  submitting.value = true
  await environmentRequest.run(async () => {
    const previous = previousDecision()
    const reply = await api.act<{ request?: string }>('/api/issues/environment', { key: issue.key, revision: issue.revision, environment })
    recordDecision(reply, previous, 'Environment preference submitted. Waiting for the supervisor to apply it.')
    environmentOpen.value = false
  })
  submitting.value = false
  if (!environmentOpen.value) await poll.refresh()
}
function closeDecision() {
  if (decisionRequest.pending.value) return
  decision.value = null
  decisionRequest.reset()
  submitting.value = false
}

async function submit({ action, reason }: { action: IssueAction; reason: string }) {
  const issue = detail.value
  if (!issue) return
  let applied = false
  const done = await decisionRequest.run(async () => {
    const previous = previousDecision()
    if (action === 'stop') {
      const before = workerState(issue.active!)
      await api.act('/api/factory/action', { action: 'stop', target: issue.active, reason })
      submitted.value = { request: null, previous }
      workerReceipt.value = { id: issue.active!, before }
    } else {
      const reply = await api.act<IssueDetailResponse & { request?: string }>('/api/issues/action', { key: issue.key, action, revision: issue.revision, reason })
      recordDecision(reply, previous)
      if (reply.issue) {
        poll.replace({ detail: reply, fence: null })
        submitted.value = null
        applied = true
      }
    }
  })
  if (!done) return
  decision.value = null
  submitting.value = false
  if (!applied) await poll.refresh()
}
// Two tabs under the issue's header: Details (everything above) and
// Discussion, a full-height chat about this issue. The tab lives in the URL
// (`?tab=discussion`), so a link opens straight into the conversation. The
// chat stays mounted while hidden, so an unsent draft and its scroll position
// survive a switch; Details gets its scroll position back here.
const router = useRouter()
const TABS = [
  { label: 'Details', value: 'details', slot: 'details' as const, class: 'pt-4' },
  { label: 'Discussion', value: 'discussion', slot: 'discussion' as const, class: 'flex min-h-0 flex-1 flex-col pt-3' },
]
const tab = ref<IssueTab>(issueTab(route.query))
// The URL follows the tab, and the tab follows the URL (back and forward),
// except while the panel's own writes are still landing: a quick switch
// back and forth must not be undone by the earlier navigation.
let writing = 0
watch(() => route.query.tab, () => { if (!writing) tab.value = issueTab(route.query) })
// What the chat reports for the tab's dot: a reply in progress, or one that
// arrived while the operator was on Details.
const chat = ref({ replying: false, unread: false })
const chatAttention = computed(() => tab.value !== 'discussion' && (chat.value.replying || chat.value.unread))
const root = ref<HTMLElement | null>(null)
let detailsScroll = 0
function scroller(): HTMLElement | null {
  let node = root.value?.parentElement ?? null
  while (node && node !== document.body) {
    if (/auto|scroll/.test(getComputedStyle(node).overflowY)) return node
    node = node.parentElement
  }
  return document.scrollingElement as HTMLElement | null
}
// The page's chat fills the window below everything above it, so the prompt
// sits at the bottom of the screen and the window itself does not scroll.
const fillHeight = ref<string | undefined>(undefined)
function measure() {
  if (props.fill !== 'viewport' || !root.value) return
  const top = root.value.getBoundingClientRect().top + window.scrollY
  fillHeight.value = `calc(100dvh - ${Math.round(top)}px - 1.5rem)`
}
// The panel is revealed a frame later (the tabs' presence transition), and a
// tab clicked with the mouse takes focus on mousedown: focus the box after.
async function openChat() {
  await nextTick()
  measure()
  for (let frame = 0; frame < 10; frame++) {
    await new Promise(resolve => requestAnimationFrame(resolve))
    if (tab.value !== 'discussion') return
    if (conversation.value?.focus()) return
  }
}
watch(tab, async (next, previous) => {
  if (previous === 'details') detailsScroll = scroller()?.scrollTop ?? 0
  if (writing || issueTab(route.query) !== next) {
    writing++
    void router.replace({ query: tabQuery(route.query, next) }).finally(() => { writing-- })
  }
  if (next === 'discussion') return openChat()
  await nextTick()
  const box = scroller()
  if (box) box.scrollTop = detailsScroll
})
// A link straight to the discussion: focus waits in the message box once the
// issue (and with it the chat) has rendered.
const stopInitialFocus = watch(conversation, (value) => {
  if (!value) return
  if (tab.value === 'discussion') void openChat()
  stopInitialFocus()
}, { flush: 'post' })
onMounted(() => window.addEventListener('resize', measure))
onBeforeUnmount(() => window.removeEventListener('resize', measure))
const filling = computed(() => !!detail.value && tab.value === 'discussion')
const rootClass = computed(() => {
  if (!filling.value) return ''
  return props.fill === 'container' ? 'flex h-full min-h-0 flex-col' : 'flex min-h-96 flex-col'
})
</script>

<template>
  <div ref="root" :class="rootClass" :style="filling && fill === 'viewport' ? { height: fillHeight } : undefined">
    <UAlert v-if="problem" role="status" color="warning" variant="subtle" :description="problem" />
    <p v-else-if="!detail" role="status" class="text-sm text-muted">Loading issue…</p>

    <template v-if="detail">
      <!-- Header: where the issue lives, what it is and its status. It
           stays above both tabs, so the chat always says which issue it is. -->
      <p class="text-xs text-muted">{{ detail.repo }} · #{{ detail.number }}</p>
      <div class="mt-1 flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1">
        <h1 id="issue-title" class="min-w-0 text-lg font-semibold text-white wrap-anywhere sm:text-xl" :class="filling ? 'line-clamp-2' : ''">{{ detail.title }}</h1>
        <UBadge
          id="issue-status"
          :color="badge.color"
          :variant="badge.dim ? 'outline' : 'subtle'"
          :icon="badge.icon ?? undefined"
          :class="badge.dim ? 'text-muted' : ''"
          :data-tone="badge.dim ? 'calm' : undefined"
        >{{ statusLabel(detail) }}</UBadge>
      </div>

      <p v-if="detail.operator_status?.reason" id="issue-status-reason" class="mt-1 text-sm text-muted">{{ detail.operator_status.reason }}</p>

      <UTabs
        :model-value="tab"
        :items="TABS"
        :unmount-on-hide="false"
        activation-mode="manual"
        variant="link"
        size="sm"
        aria-label="Issue views"
        class="mt-3"
        :class="filling ? 'min-h-0 flex-1' : ''"
        :ui="{ root: 'gap-0 items-stretch', list: 'p-0', trigger: 'min-h-11 sm:min-h-9' }"
        @update:model-value="value => tab = issueTab({ tab: String(value) })"
      >
        <template #trailing="{ item }">
          <span v-if="item.value === 'discussion' && chatAttention" id="discussion-tab-dot" class="inline-flex items-center">
            <span aria-hidden="true" class="size-2 rounded-full" :class="chat.replying ? 'bg-sky-300 motion-safe:animate-pulse' : 'bg-primary'" />
            <span class="sr-only">{{ chat.replying ? '(reply in progress)' : '(new reply)' }}</span>
          </span>
        </template>
        <template #details>
          <p v-if="pipeline" id="issue-attempt-line" class="mb-3 text-xs" :class="NOTE_TONE[pipeline.state]">
            <span id="issue-stage-note">{{ pipeline.note }}</span>
            <template v-if="pipeline.attempts">
              <span class="text-muted" aria-hidden="true"> · </span>
              <span id="issue-attempts" class="text-muted">{{ pipeline.attempts }}</span>
            </template>
          </p>

          <!-- The next step: the cube on its workbench says what is going on;
               directly under it, how to move the issue on — to completion, or
               out of Forge with Close issue. -->
          <section id="issue-next" class="flex flex-col gap-3" aria-labelledby="issue-next-title">
            <h2 id="issue-next-title" class="sr-only">Next step</h2>
            <IssueWorkbench v-if="look" :look="look">
              <p v-if="summary?.headline" id="issue-headline" class="text-[15px] leading-snug font-semibold text-balance text-white">{{ summary.headline }}</p>
              <p
                v-if="summary?.plain"
                id="issue-plain"
                class="text-sm text-[#c5cad1] wrap-anywhere"
                :class="[summary?.headline ? 'mt-1.5' : '', plainLong && !plainOpen ? 'line-clamp-3' : '']"
              >{{ summary.plain }}</p>
              <UButton
                v-if="plainLong"
                size="xs"
                color="neutral"
                variant="link"
                class="px-0"
                :aria-expanded="plainOpen"
                @click="plainOpen = !plainOpen"
              >{{ plainOpen ? 'less' : 'more' }}</UButton>
              <ul v-if="holds.length" id="issue-held-by" class="mt-1.5 text-xs text-muted">
                <li v-for="line in holds" :key="line">{{ line }}</li>
              </ul>
              <p v-if="!summary && !holds.length" class="text-sm text-muted">Forge has no summary for this issue yet.</p>
            </IssueWorkbench>

            <div id="issue-action-block" class="flex flex-col gap-2.5">
              <section v-if="detail.decision_request" id="issue-decision" class="space-y-2 text-sm" aria-labelledby="decision-question">
                <h3 id="decision-question" class="font-semibold">{{ detail.decision_request.question }}</h3>
                <ul class="list-disc space-y-1 pl-5">
                  <li v-for="option in detail.decision_request.options" :key="option">{{ option }}</li>
                </ul>
                <p><strong>Recommendation:</strong> {{ detail.decision_request.recommendation }}</p>
                <p class="text-muted">{{ detail.decision_request.evidence }}</p>
                <p class="text-muted">Discuss the options below, then choose Retry attempt and record your answer.</p>
              </section>
              <p v-if="approval && (sentToGitHub || !summary?.headline)" role="status" class="text-sm text-amber-200">Waiting for your approval on GitHub</p>
              <ul
                v-if="approvals.length"
                id="issue-approvals"
                class="flex flex-col gap-1 rounded-lg border border-line bg-panel px-2.5 py-2 text-sm"
                aria-label="Reviews Forge found on GitHub"
              >
                <li v-for="line in approvals" :key="line.login" class="flex items-start gap-1.5" :class="line.counted ? 'text-mint-200' : 'text-muted'">
                  <UIcon :name="line.icon" class="mt-0.5 size-4 shrink-0" aria-hidden="true" />
                  <span>{{ line.text }}</span>
                </li>
              </ul>
              <p v-else-if="approval" id="issue-approvals-empty" class="text-sm text-muted">No approval found on the pull request yet.</p>
              <p v-if="detail.merge_blocker" class="text-sm text-amber-200">Check the pull request checks and repository rules on GitHub. If branch rules block the controller, configure its pull-request bypass in Repository setup. Forge will retry after the blocker is resolved.</p>
              <p v-if="reviewRequested" id="review-requested" class="text-sm text-muted">{{ reviewRequested }}</p>
              <p v-if="deployment" id="issue-deployment" class="text-sm text-muted">
                Deployment · {{ deployment.text }}
                <a
                  v-if="deployment.runUrl"
                  :href="deployment.runUrl"
                  target="_blank"
                  rel="noopener noreferrer"
                  class="ml-1 text-sky-200 underline-offset-4 hover:underline"
                >Deploy run ↗</a>
              </p>

              <!-- What this issue waits for: one chip per dependency. -->
              <div v-if="detail.dependencies.length" id="issue-dependencies" class="flex flex-wrap items-center gap-1.5 text-xs text-muted">
                <span>Depends on</span>
                <NuxtLink
                  v-for="number in detail.dependencies"
                  :key="number"
                  :to="siblingLink(number)"
                  class="inline-flex items-center gap-1.5 rounded-full border border-line bg-panel py-0.5 pr-2.5 pl-1.5 text-white hover:border-muted"
                >
                  <span aria-hidden="true" class="size-3 rounded-[3px] border-[1.5px] border-[#0a0c0f] bg-[#59606b]" />
                  <span class="font-mono">#{{ number }}</span>
                </NuxtLink>
                <a :href="issueUrl" target="_blank" rel="noopener noreferrer" class="ml-1 underline underline-offset-4">Edit dependencies on GitHub ↗</a>
              </div>

              <p v-if="message" id="detail-message" role="status" class="text-sm text-amber-200">{{ message }}</p>

              <section v-if="detail.verification_request" id="issue-verification" class="space-y-2 rounded-lg border border-line bg-panel p-4 text-sm">
                <h3 class="font-medium">Verification needed</h3>
                <p class="text-muted">{{ detail.verification_request.why }}</p>
                <pre class="whitespace-pre-wrap break-words font-sans">{{ detail.verification_request.what }}</pre>
                <p v-if="detail.verification_request.ci?.length" class="text-muted">Waiting for configured CI checks: {{ detail.verification_request.ci.map(check => check.name).join(', ') }}. Forge checks them automatically.</p>
                <template v-else-if="detail.verification_request.can_confirm">
                  <p class="text-muted">Run these steps on the current pull request revision. Record what you checked and confirm only after they pass.</p>
                  <UTextarea v-model="verificationStatement" aria-label="Verification evidence" placeholder="What I checked and the result" :maxlength="2000" :disabled="busy" class="w-full" />
                  <UButton :disabled="busy || !verificationStatement.trim()" :loading="verificationRequest.pending.value" @click="confirmVerification">Confirm verification passed</UButton>
                  <p v-if="verificationRequest.error.value" role="alert" class="text-error">{{ verificationRequest.error.value }}</p>
                </template>
                <p v-else class="text-muted">Run these steps on the current PR revision, then approve on GitHub if they pass. If they fail, comment on the PR and retry with the findings.</p>
              </section>

              <!-- One primary way forward (or a calm line when there is none),
                   smaller secondary decisions, then stopping the attempt. -->
              <!-- Dropped work asks nothing: the only way back is on GitHub. -->
              <div v-if="detail.status === 'dropped'" id="issue-actions" class="flex flex-wrap items-center gap-2">
                <span id="issue-nothing-needed" data-dropped class="mr-1 inline-flex min-h-8 flex-wrap items-center gap-x-2 gap-y-0.5 text-sm text-muted">
                  <UIcon name="i-lucide-archive" class="size-4 shrink-0" aria-hidden="true" />
                  <span>Nothing needed from you.</span>
                  <a
                    id="issue-reopen"
                    :href="issueUrl"
                    target="_blank"
                    rel="noopener noreferrer"
                    class="text-sky-200 underline-offset-4 hover:underline"
                  >Reopen it on GitHub to bring it back ↗</a>
                </span>
              </div>
              <div v-else id="issue-actions" class="flex flex-wrap items-center gap-2">
                <UButton v-if="primary === 'approve'" color="primary" :disabled="busy || poll.stale.value" @click="approvalOpen = true">Approve…</UButton>
                <UButton v-else-if="primary === 'assign_reviewer'" color="primary" :disabled="busy" @click="askReviewer">{{ ACTION_LABELS.assign_reviewer }}</UButton>
                <UButton v-else-if="primary === 'setup'" :to="detail.setup_url" color="primary">Repository setup →</UButton>
                <UButton v-else-if="primary" color="primary" :disabled="busy" @click="ask(primary)">{{ ACTION_LABELS[primary] }}</UButton>
                <span v-else-if="detail.status === 'blocked'" id="issue-nothing-needed" data-blocked class="mr-1 inline-flex min-h-8 flex-wrap items-center gap-x-1 gap-y-0.5 text-sm text-muted">
                  <UIcon name="i-lucide-link-2" class="mr-1 size-4 shrink-0" aria-hidden="true" />
                  <span>Nothing needed from you.</span>
                  <template v-if="blocking.length">
                    <span>Starts by itself when</span>
                    <template v-for="(number, index) in blocking" :key="number">
                      <span class="whitespace-nowrap"><NuxtLink
                        :to="siblingLink(number)"
                        :data-blocking="number"
                        class="font-mono text-white underline-offset-4 hover:underline"
                      >#{{ number }}</NuxtLink>{{ index < blocking.length - 2 ? ',' : '' }}</span>
                      <span v-if="index === blocking.length - 2">and</span>
                    </template>
                    <span>{{ blocking.length === 1 ? 'closes.' : 'close.' }}</span>
                  </template>
                  <span v-else>Starts by itself when the issues it waits on close.</span>
                </span>
                <span v-else-if="detail.status === 'attention'" id="issue-needs-person" class="mr-1 inline-flex min-h-8 items-center gap-2 text-sm text-amber-200">
                  <span aria-hidden="true" class="size-2 shrink-0 rounded-full bg-amber-200" />
                  {{ attentionNote(detail) }}
                </span>
                <span v-else id="issue-nothing-needed" class="mr-1 inline-flex min-h-8 items-center gap-2 text-sm text-muted">
                  <span aria-hidden="true" class="size-2 rounded-full bg-mint-400" />
                  {{ look?.mood === 'happy' ? 'Done. Nothing left to do.' : 'Nothing needed from you right now.' }}
                </span>
                <UButton
                  v-for="action in secondary"
                  :key="action"
                  color="neutral"
                  variant="outline"
                  size="sm"
                  :disabled="busy"
                  @click="ask(action)"
                >{{ ACTION_LABELS[action] }}</UButton>
                <UButton
                  v-if="canAssign && primary !== 'assign_reviewer'"
                  size="sm"
                  color="neutral"
                  variant="outline"
                  :disabled="busy"
                  @click="askReviewer"
                >{{ ACTION_LABELS.assign_reviewer }}</UButton>
                <UButton
                  size="sm"
                  color="neutral"
                  variant="outline"
                  @click="tab = 'discussion'"
                >Discuss blocker</UButton>
                <UButton v-if="setupNeeded && primary !== 'setup'" :to="detail.setup_url" size="sm" color="neutral" variant="outline">Repository setup →</UButton>
                <span v-if="(detail.active && detail.status === 'working') || internalJob" class="ml-auto flex flex-wrap items-center gap-1">
                  <UButton
                    v-if="detail.active && detail.status === 'working'"
                    size="sm"
                    color="error"
                    variant="ghost"
                    :disabled="busy"
                    @click="ask('stop')"
                  >{{ ACTION_LABELS.stop }}</UButton>
                  <UButton
                    v-if="internalJob"
                    size="sm"
                    color="error"
                    variant="ghost"
                    :disabled="busy"
                    @click="askReset"
                  >Reset runner</UButton>
                </span>
              </div>

              <!-- The quiet way out, and how fresh this is. -->
              <div id="issue-meta" class="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 border-t border-dashed border-line pt-2 text-xs text-muted">
                <UButton
                  v-if="detail.closable"
                  id="issue-close"
                  size="xs"
                  color="error"
                  variant="ghost"
                  icon="i-lucide-circle-x"
                  class="-ml-2"
                  :disabled="busy"
                  @click="ask('close')"
                >Close issue…</UButton>
                <span v-else />
                <div id="issue-checked" class="flex flex-wrap items-center gap-1">
                  <span v-if="checkingSince" role="status">Checking GitHub…</span>
                  <span v-else-if="checked">{{ checked }}</span>
                  <span v-if="checked || checkingSince" aria-hidden="true">·</span>
                  <UButton size="xs" color="neutral" variant="ghost" icon="i-lucide-refresh-cw" class="-mx-1" :loading="checkRequest.pending.value || !!checkingSince" :disabled="checkRequest.pending.value || !!checkingSince" @click="checkNow">Check now</UButton>
                  <span v-if="checkNote" role="status" class="basis-full">{{ checkNote }}</span>
                </div>
              </div>
            </div>
          </section>

          <IssueStats v-if="look" :look="look" class="mt-4" />

          <!-- Everything else, quieter: where the work lives on GitHub, then
               collapsed sections. -->
          <p id="issue-links" class="mt-4 flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
            <a :href="issueUrl" target="_blank" rel="noopener noreferrer" class="text-sky-200 underline-offset-4 hover:underline">GitHub issue ↗</a>
            <a
              v-for="pull in detail.pull_requests"
              :key="pull.number"
              :href="pullUrl(pull.number)"
              :title="pull.title"
              target="_blank"
              rel="noopener noreferrer"
              class="text-sky-200 underline-offset-4 hover:underline"
            >{{ pullLabel(pull) }} ↗</a>
          </p>

          <div class="mt-3">
            <IssueSection v-if="hasFindings" id="issue-details" v-model:open="detailsOpen" title="Review findings">
              <p v-if="summary?.details" id="issue-explanation" class="whitespace-pre-line text-sm text-muted wrap-anywhere">{{ summary.details }}</p>
              <div v-if="reviews.length" id="issue-reviews" class="mt-3 flex flex-col gap-2">
                <div v-for="row in reviews" :key="row.role" class="rounded-xl border border-line bg-panel px-3 py-2">
                  <div class="flex flex-wrap items-center gap-2">
                    <UBadge :color="row.tone" variant="subtle" size="sm">{{ row.outcomeLabel }}</UBadge>
                    <span class="text-sm font-semibold text-white">{{ row.label }}</span>
                    <UButton
                      v-if="row.more"
                      size="xs"
                      color="neutral"
                      variant="ghost"
                      class="ml-auto"
                      @click="toggleReview(row.role)"
                    >{{ expanded.has(row.role) ? 'Less' : 'More' }}</UButton>
                  </div>
                  <p class="mt-1 text-sm text-muted wrap-anywhere" :class="expanded.has(row.role) ? '' : 'line-clamp-2'">
                    {{ expanded.has(row.role) ? row.summary : row.lead }}
                  </p>
                </div>
              </div>
            </IssueSection>

            <IssueSection v-if="detail.environment" id="issue-environment-section" v-model:open="environmentSectionOpen" title="Worker environment">
              <IssueEnvironmentCard
                v-model:open="environmentOpen"
                :environment="detail.environment"
                :busy="busy"
                :error="environmentRequest.error.value ?? ''"
                @save="saveEnvironment"
              />
            </IssueSection>

            <!-- Collapsed by default but always rendered, so the server-rendered
                 description is in the page from the first paint. -->
            <IssueSection id="issue-description" v-model:open="descriptionOpen" title="Description" :unmount-on-hide="false">
              <div class="rounded-xl border border-line bg-panel p-4">
                <!-- Loaded lazily on navigation: say nothing until it arrives, not "no description". -->
                <MarkdownBody v-if="html != null" :html="html" />
                <p v-if="detail.body_truncated" class="mt-2 text-xs text-amber-200">Description shortened. Open on GitHub to read the full issue.</p>
              </div>
            </IssueSection>
          </div>

          <IssueWorkerOutput v-model:open="outputOpen" :jobs="jobs" :selected-job="selectedJob" />
          <IssueSection id="issue-activity" v-model:open="activityOpen" title="Recent activity" class="border-b">
            <ul v-if="detail.history.length" class="flex flex-col gap-2 text-xs">
              <li v-for="(event, index) in detail.history" :key="`${event.at}-${index}`">
                <time :datetime="isoTime(event.at)" class="text-muted">{{ localTime(event.at) }}</time>
                <span class="ml-2 text-white">{{ eventLabel(event.kind) }}</span>
                <p v-if="event.reason" class="text-muted wrap-anywhere">{{ event.reason }}</p>
              </li>
            </ul>
            <p v-else class="text-xs text-muted">No Forge activity yet.</p>
          </IssueSection>
        </template>
        <template #discussion>
          <IssueConversation
            ref="conversation"
            :key="detail.key"
            :active="tab === 'discussion'"
            :issue-key="detail.key"
            :actions="decisions"
            :primary="typeof primary === 'string' && primary !== 'approve' && primary !== 'setup' ? primary : null"
            :disabled="busy"
            @act="(action, reason) => action === 'assign_reviewer' ? askReviewer() : ask(action, reason)"
            @attention="value => chat = value"
          />
        </template>
      </UTabs>

      <LazyApprovalDialog
        v-if="approvalOpen && approval && detail"
        :reason="approval.reason"
        :verification="!!detail.verification_request"
        :url="pullUrl(approval.pr) + '/files'"
        :can-assign="canAssign"
        @opened="sentToGitHub = true"
        @close="approvalOpen = false"
        @assign="askReviewer"
      />
      <ReviewerDialog
        v-if="assigning"
        :open="assigning"
        :reviewers="detail.reviewers ?? []"
        :preselected="preselected"
        :pull="candidatePull"
        :pull-url="candidateUrl"
        :error="assignRequest.error.value"
        :pending="assignRequest.pending.value"
        @submit="assignReviewer"
        @close="closeReviewer"
      />

      <CloseIssueDialog
        v-if="closeOpen"
        :open="closeOpen"
        :issue-key="detail.key"
        :number="detail.number"
        :pull="candidatePull?.number ?? null"
        :error="decisionRequest.error.value"
        :pending="decisionRequest.pending.value"
        @submit="submit"
        @close="closeDecision"
      />
      <DecisionPanel
        v-if="decision && decision !== 'close'"
        :open="!!decision"
        :action="decision"
        :detail="detail"
        :error="decisionRequest.error.value"
        :pending="decisionRequest.pending.value"
        :reason="decisionReason"
        @submit="submit"
        @close="closeDecision"
      />
    </template>

    <!-- An internal run whose issue is not in the inventory keeps its one
         control and its output: the bar above belongs to the issue, this one
         to the run. -->
    <template v-else>
      <div v-if="internalJob" id="job-actions" class="mt-4 flex flex-wrap items-center gap-2">
        <UButton size="sm" color="error" variant="ghost" :disabled="busy" @click="askReset">Reset runner</UButton>
      </div>
      <IssueWorkerOutput v-if="showOutput" v-model:open="outputOpen" :jobs="jobs" :selected-job="selectedJob" class="mt-4" />
    </template>

    <!-- Reset targets the run, not the issue, so it lives outside the block
         the issue's own decisions are rendered in. -->
    <ConfirmDialog
      v-if="resetting"
      :open="resetting"
      :title="RESET.title"
      :text="RESET.text"
      :confirm-label="RESET.confirmLabel"
      :error="resetRequest.error.value"
      :pending="resetRequest.pending.value"
      pending-label="Resetting…"
      @confirm="confirmReset"
      @close="closeReset"
    />
  </div>
</template>
