<script setup lang="ts">
import { ForgeApiError } from '~/composables/useForgeApi'
import type { FactoryView, JobView } from '~/utils/factory'
import {
  ACTION_LABELS, STATUS_NAMES, eventLabel, issuePath, lastAction, pendingAction,
  type IssueAction, type IssueDetail as IssueDetailView, type IssueDetailResponse,
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

async function load(): Promise<Loaded> {
  const [detail, factory] = await Promise.all([
    api.get<IssueDetailResponse>(`/api/issues?issue=${encodeURIComponent(props.issueKey)}`, { timeout: 30000 })
      .then(value => ({ value, fence: null as string | null }))
      .catch((error: unknown) => {
        // A 200 {ok:false} is a state (the reset fence); a 404 is an issue
        // that is not in a managed repository any more.
        if (error instanceof ForgeApiError && error.status === 200) return { value: null, fence: error.message }
        if (error instanceof ForgeApiError && error.status === 404) return { value: null, fence: UNMANAGED }
        throw error
      }),
    api.get<FactoryView>('/api/factory', { timeout: 30000 }).catch(() => null),
  ])
  return { detail: detail.value, fence: detail.fence, factory }
}

// Rendered on the server for the first paint (the shared link lands here);
// the poll takes over on the client.
const { data: initial } = await useAsyncData(`issue-${props.issueKey}`, load)

const poll = usePoll<Loaded>(load, {
  interval: useRuntimeConfig().public.pollInterval,
  allowed: () => !submitting.value,
})

const mounted = ref(false)
onMounted(() => {
  mounted.value = true
  if (!poll.data.value && initial.value) poll.data.value = initial.value
  poll.start()
})

const loaded = computed<Loaded | null>(() => poll.data.value ?? initial.value ?? null)
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

const explanation = computed(() => {
  const issue = detail.value
  if (!issue) return ''
  return [issue.explanation, issue.environment_blocker].filter(Boolean).join(' ')
})

const issueUrl = computed(() => (detail.value ? `https://github.com/${detail.value.repo}/issues/${detail.value.number}` : ''))
function pullUrl(number: number) { return `https://github.com/${detail.value?.repo}/pull/${number}` }

function reviewLine(review: { role: string; outcome: string | null; summary: string }) {
  const label = review.role === 'review' ? 'Review' : 'Security review'
  const outcome = review.outcome === 'passed' ? 'passed' : 'needs attention'
  return `${label}: ${outcome} · ${review.summary}`
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

const selectedJob = computed<string | null>(() => {
  const asked = route.query.job
  const job = Array.isArray(asked) ? asked[0] : asked
  return (typeof job === 'string' && job) || detail.value?.active || null
})

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
      <p class="mt-2 text-sm text-muted">{{ explanation }}</p>
      <p v-if="message" id="detail-message" role="status" class="mt-2 text-sm text-amber-200">{{ message }}</p>

      <p v-for="review in detail.reviews" :key="review.role" class="mt-2 text-sm text-muted">{{ reviewLine(review) }}</p>

      <div class="mt-4 flex flex-wrap items-center gap-2">
        <UButton
          v-for="(action, index) in detail.actions"
          :key="action"
          :color="index === 0 && PRIMARY_ACTIONS.includes(action) ? 'primary' : 'neutral'"
          :variant="index === 0 && PRIMARY_ACTIONS.includes(action) ? 'solid' : 'outline'"
          size="sm"
          :disabled="busy"
          @click="ask(action)"
        >{{ ACTION_LABELS[action] }}</UButton>
        <UButton v-if="detail.setup_needed" :to="detail.setup_url" size="sm" color="neutral" variant="outline">Repository setup →</UButton>
        <UButton
          v-if="detail.active && detail.status === 'working'"
          size="sm"
          color="error"
          variant="ghost"
          :disabled="busy"
          @click="ask('stop')"
        >{{ ACTION_LABELS.stop }}</UButton>
      </div>

      <section class="mt-5 rounded-xl border border-line bg-panel p-4">
        <MarkdownBody :html="html ?? ''" :truncated="detail.body_truncated" />
      </section>

      <div class="mt-4 flex flex-wrap items-center gap-x-4 gap-y-2 text-sm">
        <a :href="issueUrl" target="_blank" rel="noopener noreferrer" class="text-sky-200 hover:underline">Open on GitHub ↗</a>
        <a
          v-for="pull in detail.pull_requests"
          :key="pull.number"
          :href="pullUrl(pull.number)"
          target="_blank"
          rel="noopener noreferrer"
          class="text-sky-200 hover:underline"
        >Pull request #{{ pull.number }} ↗</a>
      </div>

      <p v-if="detail.dependencies.length" class="mt-2 text-sm text-muted">
        Depends on
        <template v-for="(number, index) in detail.dependencies" :key="number">
          <NuxtLink :to="issuePath(`${detail.repo_id}:${number}`)" class="text-sky-200 hover:underline">#{{ number }}</NuxtLink>{{ index < detail.dependencies.length - 1 ? ', ' : '' }}
        </template>
      </p>

      <section class="mt-5 rounded-xl border border-line bg-bg p-3">
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
              :to="{ query: { job: job.id } }"
              :aria-current="job.id === selectedJob ? 'true' : undefined"
              class="hover:underline"
              :class="job.id === selectedJob ? 'text-mint-200' : 'text-muted'"
            >{{ jobLine(job) }}<span v-if="job.started_at"> · <time :datetime="isoTime(job.started_at)">{{ localTime(job.started_at) }}</time></span></NuxtLink>
          </li>
        </ul>
      </section>

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
  </div>
</template>
