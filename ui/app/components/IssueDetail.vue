<script setup lang="ts">
import { ForgeApiError } from '~/composables/useForgeApi'
import { deploymentLine, type FactoryView, type JobView } from '~/utils/factory'
import {
  ACTION_LABELS, STATUS_NAMES, eventLabel, issuePath, lastAction, pendingAction,
  preselectedReviewers, reviewRequestText, reviewRows, reviewerOptions, reviewerReason, summaryBlock,
  type IssueAction, type IssueDetail as IssueDetailView, type IssueDetailResponse, type ReviewOutcome,
} from '~/utils/issues'

// The one detail view: /issues/<key> owns the issue's decisions and its
// worker output. It polls the issue itself and the factory (for the issue's
// jobs, which the issue payload does not carry), and it owns both POSTs —
// DecisionPanel only reports what the operator confirmed.
const props = defineProps<{ issueKey: string }>()

const UNMANAGED = 'This issue is not in a managed repository. Refresh the issue list.'
const STALE = 'Showing the last received issue. Refresh before making a decision.'
const PRIMARY_ACTIONS: IssueAction[] = ['queue', 'resume', 'retry', 'recheck']

const api = useForgeApi()
const route = useRoute()

interface Loaded { detail: IssueDetailResponse | null; fence: string | null; factory: FactoryView | null }

// True while a decision dialog is open or a POST is in flight, so a poll can
// never re-render the issue under the operator's hands.
const submitting = ref(false)

const factory = useFactory()
const poll = useResource<Omit<Loaded, 'factory'>>(() => `/api/issues?issue=${encodeURIComponent(props.issueKey)}`, async (path) => {
  try { return { detail: await api.get<IssueDetailResponse>(path, { timeout: 30000 }), fence: null } }
  catch (error) {
    if (error instanceof ForgeApiError && error.status === 200) return { detail: null, fence: error.message }
    if (error instanceof ForgeApiError && error.status === 404) return { detail: null, fence: UNMANAGED }
    throw error
  }
}, { allowed: () => !submitting.value })
// Warm detail opens synchronously; cold direct links retain SSR data.
const { data: initial } = await useAsyncData(`issue-${props.issueKey}`, () => poll.refresh(5000))
if (!poll.data.value && initial.value) poll.replace(initial.value)
const mounted = ref(false)
onMounted(() => { mounted.value = true; poll.start(); factory.start() })
const loaded = computed<Loaded | null>(() => {
  const value = poll.data.value
  return value ? { ...value, factory: factory.data.value?.ok === false ? null : factory.data.value } : null
})
const detail = computed<IssueDetailView | null>(() => loaded.value?.detail?.issue ?? null)
const actions = computed(() => loaded.value?.detail?.actions ?? [])
const fence = computed(() => loaded.value?.fence ?? null)

// Nothing to show yet: either the fence/404 copy, or the failure of a first
// load that never returned an issue to fall back on.
const problem = computed(() => fence.value ?? (detail.value ? null : poll.error.value))

const pending = computed(() => (detail.value ? pendingAction(actions.value, detail.value.key) : null))
const busy = computed(() => submitting.value || !!pending.value)

// Description: rendered and sanitized server-side, re-rendered whenever the
// body changes (an operator edit on GitHub arrives through the poll).
const { data: html } = await useAsyncData(`md-${props.issueKey}`, async () => {
  const issue = detail.value
  if (!issue?.body) return ''
  const rendered = await $fetch<{ html: string }>('/_ui/markdown', { method: 'POST', body: { text: issue.body, repo: issue.repo } })
  return rendered.html
}, { watch: [() => detail.value?.body] })

const submitted = ref('')
const dialogError = ref<string | null>(null)
const decision = ref<IssueAction | null>(null)

// One message line, in the order the operator needs it: what Forge is doing
// with the last decision, then whether what they are looking at is current,
// then the receipt for what they just submitted.
const message = computed(() => {
  const last = detail.value ? lastAction(actions.value, detail.value.key) : null
  if (last) return last.status === 'pending' ? 'Applying your decision…' : (last.message ?? '')
  if (poll.stale.value) return STALE
  return submitted.value
})

// What the page leads with: one headline sentence, then at most three
// sentences. Forge's own status line and the worker's prose move into Details,
// so nobody has to open it to learn where this issue stands or why.
const summary = computed(() => summaryBlock(detail.value))
const detailsOpen = useState(`issue-details-${props.issueKey}`, () => false)
const hasDetails = computed(() => !!summary.value?.details || reviews.value.length > 0)

const issueUrl = computed(() => (detail.value ? `https://github.com/${detail.value.repo}/issues/${detail.value.number}` : ''))
function pullUrl(number: number) { return `https://github.com/${detail.value?.repo}/pull/${number}` }

// Assigning a human reviewer is the one decision with an argument, so its
// control lives in the action bar rather than behind a confirmation dialog:
// the operator picks the people, then confirms. A login the collaborator
// listing does not know is still accepted — GitHub validates it — and a
// listing that failed leaves the free-text box as the only input.
const reviewerChoices = computed(() => reviewerOptions(detail.value?.reviewers))
const canAssign = computed(() => !!detail.value?.actions.includes('assign_reviewer'))
const decisions = computed(() => (detail.value?.actions ?? []).filter(action => action !== 'assign_reviewer'))
const chosenReviewers = ref<string[]>([])
const typedReviewers = ref('')
const assignError = ref<string | null>(null)
const preselected = ref<string | null>(null)

watchEffect(() => {
  const issue = detail.value
  if (!issue || preselected.value === issue.key || !issue.actions.includes('assign_reviewer')) return
  preselected.value = issue.key
  chosenReviewers.value = preselectedReviewers(issue)
})

const reviewerLogins = computed(() => (reviewerChoices.value.length
  ? chosenReviewers.value
  : typedReviewers.value.split(/[\s,;]+/)).filter(Boolean))

// "· 12 min ago" needs a clock the server does not have, so the age only
// appears once mounted; the sentence itself renders either way.
const now = ref(0)
let ticker: ReturnType<typeof setInterval> | undefined
onMounted(() => {
  now.value = Math.floor(Date.now() / 1000)
  ticker = setInterval(() => { now.value = Math.floor(Date.now() / 1000) }, 30000)
})
onBeforeUnmount(() => clearInterval(ticker))
const reviewRequested = computed(() => reviewRequestText(detail.value?.reviewers_requested, now.value))

// Whether the merge reached production is the ledger's verdict, which travels
// on the factory view rather than on the issue payload — the same view this
// page already polls for the issue's jobs.
const deployment = computed(() => {
  const factory = loaded.value?.factory ?? null
  return deploymentLine((factory?.issues ?? []).find(issue => issue.key === props.issueKey), factory, now.value)
})

async function assignReviewer() {
  const issue = detail.value
  if (!issue || !reviewerLogins.value.length) return
  assignError.value = null
  submitting.value = true
  try {
    await api.act('/api/issues/action', {
      key: issue.key, action: 'assign_reviewer', revision: issue.revision, reason: reviewerReason(reviewerLogins.value),
    })
  } catch (error: unknown) {
    assignError.value = error instanceof Error ? error.message : 'Forge did not accept the action.'
    return
  } finally {
    submitting.value = false
  }
  submitted.value = 'Decision submitted. Waiting for confirmation.'
  await poll.refresh()
}

// Reviews are compact rows — the verdict, the role, the lead sentence — inside
// Details; "More" reveals the worker's full text. The chip is that reviewer's
// verdict, never the issue's status, which the badge in the title line carries.
const reviews = computed(() => reviewRows(detail.value?.reviews))
const REVIEW_COLORS: Record<ReviewOutcome, 'success' | 'error' | 'warning' | 'neutral'> = {
  passed: 'success', rejected: 'error', unable: 'warning', unknown: 'neutral',
}
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
const jobs = computed<JobView[]>(() => (loaded.value?.factory?.jobs ?? [])
  .filter(job => job.issue_key === props.issueKey)
  .slice()
  .sort((left, right) => (right.started_at ?? right.created_at) - (left.started_at ?? left.created_at)))

// `?job=<id>` is a link to one run — what the operator follows out of the
// activity list or a shared message.
const requestedJob = computed<string | null>(() => {
  const asked = route.query.job
  const job = Array.isArray(asked) ? asked[0] : asked
  return typeof job === 'string' && job ? job : null
})
const selectedJob = computed<string | null>(() => requestedJob.value ?? detail.value?.active ?? null)

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

const RESET = {
  title: 'Reset runner?',
  text: 'This terminates the old execution and discards its disposable workspace. Capacity is released only after teardown is verified.',
  confirmLabel: 'Reset runner',
}
const resetting = ref(false)
const resetError = ref<string | null>(null)

function askReset() {
  resetError.value = null
  resetting.value = true
  submitting.value = true
}
function closeReset() {
  resetting.value = false
  submitting.value = false
}
async function confirmReset(reason: string) {
  const job = internalJob.value
  if (!job) return
  resetError.value = null
  try {
    await api.act('/api/factory/action', { action: 'reset', target: job.id, reason })
  } catch (error: unknown) {
    resetError.value = error instanceof Error ? error.message : 'Forge did not accept the action.'
    return
  }
  closeReset()
  submitted.value = 'Decision submitted. Waiting for confirmation.'
  await poll.refresh()
}

function jobLine(job: JobView): string {
  const outcome = job.outcome ?? (job.cleanup ? 'cleanup' : job.phase)
  return `${job.role} · ${job.phase} · ${outcome}`
}

const outputOpen = ref(false)
const activityOpen = ref(false)
const logs = ref<string | null>(null)
const loadedJob = ref<string | null>(null)

async function loadOutput() {
  const id = selectedJob.value
  if (!id || loadedJob.value === id) return
  loadedJob.value = id
  try {
    const result = await api.get<{ ok: true; logs: string }>(`/api/factory/output?id=${encodeURIComponent(id)}`, { timeout: 15000 })
    logs.value = result.logs ?? ''
  } catch {
    logs.value = ''
  }
}

watch(outputOpen, (open) => { if (open) void loadOutput() })
watch(selectedJob, () => { loadedJob.value = null; logs.value = null; if (outputOpen.value) void loadOutput() })

function ask(action: IssueAction) {
  dialogError.value = null
  decision.value = action
  submitting.value = true
}
function closeDecision() {
  decision.value = null
  dialogError.value = null
  submitting.value = false
}

async function submit({ action, reason }: { action: IssueAction; reason: string }) {
  const issue = detail.value
  if (!issue) return
  dialogError.value = null
  try {
    if (action === 'stop') await api.act('/api/factory/action', { action: 'stop', target: issue.active, reason })
    else await api.act('/api/issues/action', { key: issue.key, action, revision: issue.revision, reason })
  } catch (error: unknown) {
    dialogError.value = error instanceof Error ? error.message : 'Forge did not accept the action.'
    return
  }
  decision.value = null
  submitting.value = false
  submitted.value = 'Decision submitted. Waiting for confirmation.'
  await poll.refresh()
}
</script>

<template>
  <div>
    <UAlert v-if="problem" role="status" color="warning" variant="subtle" :description="problem" />
    <p v-else-if="!detail" role="status" class="text-sm text-muted">Loading issue…</p>

    <template v-if="detail">
      <p class="text-xs text-muted">{{ detail.repo }} · #{{ detail.number }}</p>
      <div class="mt-1 flex flex-wrap items-center gap-3">
        <h1 id="issue-title" class="text-2xl font-semibold text-white">{{ detail.title }}</h1>
        <UBadge color="neutral" variant="subtle">{{ STATUS_NAMES[detail.status] }}</UBadge>
      </div>
      <p v-if="summary?.headline" id="issue-headline" class="mt-2 text-base font-semibold text-white">{{ summary.headline }}</p>
      <p v-if="summary?.plain" id="issue-plain" class="mt-1 text-sm text-muted">{{ summary.plain }}</p>
      <p v-if="reviewRequested" id="review-requested" class="mt-1 text-sm text-muted">{{ reviewRequested }}</p>
      <p v-if="deployment" id="issue-deployment" class="mt-1 text-sm text-muted">
        Deployment · {{ deployment.text }}
        <a
          v-if="deployment.runUrl"
          :href="deployment.runUrl"
          target="_blank"
          rel="noopener noreferrer"
          class="ml-1 text-sky-200 underline-offset-4 hover:underline"
        >Deploy run ↗</a>
      </p>
      <p v-if="message" id="detail-message" role="status" class="mt-2 text-sm text-amber-200">{{ message }}</p>

      <!-- One action bar under the title: what the operator can decide, then
           where the work lives on GitHub, then stopping the attempt. -->
      <div id="issue-actions" class="mt-4 flex flex-wrap items-center gap-2">
        <UButton
          v-for="(action, index) in decisions"
          :key="action"
          :color="index === 0 && PRIMARY_ACTIONS.includes(action) ? 'primary' : 'neutral'"
          :variant="index === 0 && PRIMARY_ACTIONS.includes(action) ? 'solid' : 'outline'"
          size="sm"
          :disabled="busy"
          @click="ask(action)"
        >{{ ACTION_LABELS[action] }}</UButton>
        <template v-if="canAssign">
          <USelectMenu
            v-if="reviewerChoices.length"
            v-model="chosenReviewers"
            :items="reviewerChoices"
            value-key="value"
            multiple
            size="sm"
            aria-label="Reviewers"
            placeholder="Choose reviewers"
            class="min-w-[220px]"
            :disabled="busy"
          >
            <template #trailing><SelectChevron /></template>
          </USelectMenu>
          <UInput
            v-else
            v-model="typedReviewers"
            size="sm"
            aria-label="Reviewers"
            placeholder="GitHub login"
            class="min-w-[180px]"
            :disabled="busy"
          />
          <UButton
            size="sm"
            color="primary"
            :disabled="busy || !reviewerLogins.length"
            @click="assignReviewer"
          >{{ ACTION_LABELS.assign_reviewer }}</UButton>
        </template>
        <UButton v-if="detail.setup_needed" :to="detail.setup_url" size="sm" color="neutral" variant="outline">Repository setup →</UButton>
        <UButton
          :to="issueUrl"
          target="_blank"
          rel="noopener noreferrer"
          external
          size="sm"
          color="neutral"
          variant="outline"
          icon="i-lucide-arrow-up-right"
          trailing
        >GitHub</UButton>
        <UButton
          v-for="pull in detail.pull_requests"
          :key="pull.number"
          :to="pullUrl(pull.number)"
          :title="pull.title"
          target="_blank"
          rel="noopener noreferrer"
          external
          size="sm"
          color="neutral"
          variant="outline"
          icon="i-lucide-arrow-up-right"
          trailing
        >PR #{{ pull.number }}</UButton>
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
      </div>

      <p v-if="assignError" role="alert" class="mt-2 text-sm text-rose-200">{{ assignError }}</p>

      <!-- Everything the worker wrote stays available, one click away. -->
      <IssueConversation :key="detail.key" :issue-key="detail.key" />

      <section v-if="hasDetails" id="issue-details" class="mt-4 rounded-xl border border-line bg-bg p-3">
        <UCollapsible v-model:open="detailsOpen">
          <UButton color="neutral" variant="ghost" size="sm" class="w-full justify-between">
            Details
            <SelectChevron :class="detailsOpen ? 'rotate-180' : ''" />
          </UButton>
          <template #content>
            <p v-if="summary?.details" id="issue-explanation" class="mt-2 text-sm text-muted">{{ summary.details }}</p>
            <div v-if="reviews.length" id="issue-reviews" class="mt-3 flex flex-col gap-2">
              <div v-for="row in reviews" :key="row.role" class="rounded-xl border border-line bg-panel px-3 py-2">
                <div class="flex flex-wrap items-center gap-2">
                  <UBadge :color="REVIEW_COLORS[row.outcome]" variant="subtle" size="sm">{{ row.outcomeLabel }}</UBadge>
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
                <p class="mt-1 text-sm text-muted" :class="expanded.has(row.role) ? '' : 'line-clamp-2'">
                  {{ expanded.has(row.role) ? row.summary : row.lead }}
                </p>
              </div>
            </div>
          </template>
        </UCollapsible>
      </section>

      <section class="mt-5 rounded-xl border border-line bg-panel p-4">
        <MarkdownBody :html="html ?? ''" />
        <p v-if="detail.body_truncated" class="mt-2 text-xs text-amber-200">Description shortened. Open on GitHub to read the full issue.</p>
      </section>

      <p v-if="detail.dependencies.length" class="mt-2 text-sm text-muted">
        Depends on
        <template v-for="(number, index) in detail.dependencies" :key="number">
          <NuxtLink :to="issuePath(`${detail.repo_id}:${number}`)" class="text-sky-200 hover:underline">#{{ number }}</NuxtLink>{{ index < detail.dependencies.length - 1 ? ', ' : '' }}
        </template>
      </p>
    </template>

    <!-- An internal run whose issue is not in the inventory keeps its one
         control: the bar above belongs to the issue, this one to the run. -->
    <div v-if="internalJob && !detail" id="job-actions" class="mt-4 flex flex-wrap items-center gap-2">
      <UButton size="sm" color="error" variant="ghost" :disabled="busy" @click="askReset">Reset runner</UButton>
    </div>

    <!-- Outside the detail block on purpose: see showOutput. -->
    <section v-if="showOutput" class="mt-5 rounded-xl border border-line bg-bg p-3">
      <UCollapsible v-model:open="outputOpen">
        <UButton color="neutral" variant="ghost" size="sm" class="w-full justify-between">
          Worker output
          <SelectChevron :class="outputOpen ? 'rotate-180' : ''" />
        </UButton>
        <template #content>
          <pre
            v-if="logs && logs.trim()"
            class="mt-2 max-h-96 overflow-auto whitespace-pre-wrap rounded-lg border border-line bg-panel p-3 text-xs"
          >{{ logs }}</pre>
          <p v-else class="mt-2 text-xs text-muted">No captured output is available yet.</p>
        </template>
      </UCollapsible>
      <ul v-if="jobs.length" id="issue-jobs" class="mt-3 flex flex-col gap-1 text-xs">
        <li v-for="job in jobs" :key="job.id">
          <NuxtLink
            :to="{ query: { ...route.query, job: job.id } }"
            :aria-current="job.id === selectedJob ? 'true' : undefined"
            class="hover:underline"
            :class="job.id === selectedJob ? 'text-mint-200' : 'text-muted'"
          >{{ jobLine(job) }}<span v-if="job.started_at"> · <time :datetime="isoTime(job.started_at)">{{ localTime(job.started_at) }}</time></span></NuxtLink>
        </li>
      </ul>
    </section>

    <template v-if="detail">
      <section class="mt-3 rounded-xl border border-line bg-bg p-3">
        <UCollapsible v-model:open="activityOpen">
          <UButton color="neutral" variant="ghost" size="sm" class="w-full justify-between">
            Recent activity
            <SelectChevron :class="activityOpen ? 'rotate-180' : ''" />
          </UButton>
          <template #content>
            <ul v-if="detail.history.length" class="mt-2 flex flex-col gap-2 text-xs">
              <li v-for="(event, index) in detail.history" :key="`${event.at}-${index}`">
                <time :datetime="isoTime(event.at)" class="text-muted">{{ localTime(event.at) }}</time>
                <span class="ml-2 text-white">{{ eventLabel(event.kind) }}</span>
                <p v-if="event.reason" class="text-muted">{{ event.reason }}</p>
              </li>
            </ul>
            <p v-else class="mt-2 text-xs text-muted">No Forge activity yet.</p>
          </template>
        </UCollapsible>
      </section>

      <DecisionPanel
        v-if="decision"
        :open="!!decision"
        :action="decision"
        :detail="detail"
        :error="dialogError"
        @submit="submit"
        @close="closeDecision"
      />
    </template>

    <!-- Reset targets the run, not the issue, so it lives outside the block
         the issue's own decisions are rendered in. -->
    <ConfirmDialog
      v-if="resetting"
      :open="resetting"
      :title="RESET.title"
      :text="RESET.text"
      :confirm-label="RESET.confirmLabel"
      :error="resetError"
      @confirm="confirmReset"
      @close="closeReset"
    />
  </div>
</template>
