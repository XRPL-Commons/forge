<script setup lang="ts">
import { ForgeApiError } from '~/composables/useForgeApi'
import {
  SHOW_OPTIONS, countText, emptyState, freshnessText, issuePath, listParams, pendingAction,
  type IssueListResponse,
} from '~/utils/issues'

useHead({ title: 'Issues' })

const route = useRoute()
const api = useForgeApi()

// The API hands the UI `/issues?issue=<key>` in `operator.action_url` and in
// older deep links. The issue has its own route now, so that shape is
// redirected once, on the server, and only one URL is ever rendered.
const asked = Array.isArray(route.query.issue) ? route.query.issue[0] : route.query.issue
const redirecting = typeof asked === 'string' && asked.length > 0
if (redirecting) await navigateTo(issuePath(asked as string), { redirectCode: 302, replace: true })

interface Loaded { list: IssueListResponse | null; fence: string | null }

const show = useState('issues-show', () => 'open')
const repoValue = useState('issues-repoValue', () => '')
const search = useState('issues-search', () => '')
const query = useState('issues-query', () => '')
const page = useState('issues-page', () => 1)

const repoId = computed(() => (repoValue.value ? Number(repoValue.value) : null))
const params = computed(() => listParams({ state: show.value, repoId: repoId.value, q: query.value, page: page.value }))

// True while a refresh is in flight, so a poll never lands in the middle of
// one.
const submitting = ref(false)
const actionError = ref('')

const poll = useResource<Loaded>(() => `/api/issues?${params.value}`, async (path) => {
  try {
    return { list: await api.get<IssueListResponse>(path, { timeout: 30000 }), fence: null }
  } catch (error: unknown) {
    // A 200 {ok:false} is a state, not a failed request: the reset fence
    // replaces the list instead of leaving a stale one on screen.
    if (error instanceof ForgeApiError && error.status === 200) return { list: null, fence: error.message }
    throw error
  }
}, {
  interval: useRuntimeConfig().public.pollInterval,
  allowed: () => !submitting.value,
})

onMounted(() => { if (!redirecting) poll.start() })

// "Updated 3 h ago" on each card; minute-level ages only need a slow tick.
const now = useClock(30000)

// The search box is debounced so a typed word is one request, not one per
// keystroke, and any filter change starts again at page 1.
let debounce: ReturnType<typeof setTimeout> | undefined
watch(search, (value) => {
  clearTimeout(debounce)
  debounce = setTimeout(() => { query.value = value.trim(); page.value = 1 }, 200)
})
watch([show, repoValue], () => { page.value = 1 })

onBeforeUnmount(() => { clearTimeout(debounce) })

const list = computed(() => poll.data.value?.list ?? null)
const fence = computed(() => poll.data.value?.fence ?? null)
const rows = computed(() => list.value?.issues ?? [])
const empty = computed(() => (list.value ? emptyState(list.value) : null))

const repoOptions = computed(() => [
  { value: '', label: 'All repositories' },
  ...(list.value?.repositories ?? []).map(scope => ({ value: String(scope.id), label: scope.name })),
])

const freshness = computed(() => {
  if (actionError.value) return actionError.value
  if (poll.error.value) return poll.error.value
  if (fence.value) return ''
  if (!list.value) return 'Loading issues…'
  return freshnessText(list.value)
})
const freshnessWarning = computed(() => !!actionError.value || !!poll.error.value
  || (!!list.value && !list.value.complete) || (list.value?.repositories ?? []).some(scope => !!scope.error))

async function refreshInventory() {
  submitting.value = true
  actionError.value = ''
  try {
    await api.act('/api/issues/refresh', {})
  } catch (error: unknown) {
    actionError.value = error instanceof Error ? error.message : 'Forge did not accept the action.'
  } finally {
    submitting.value = false
  }
  await poll.refresh()
}

function goto(next: number) {
  const pages = Math.max(list.value?.pages ?? 1, 1)
  page.value = Math.min(Math.max(next, 1), pages)
}
</script>

<template>
  <div>
    <div class="flex flex-wrap items-end justify-between gap-3">
      <div>
        <p class="text-xs uppercase tracking-wide text-muted">Your work</p>
        <h1 class="text-3xl font-semibold text-white">Issues</h1>
        <p class="mt-1 text-sm text-muted">Choose what Forge should work on. Review anything that needs a decision.</p>
      </div>
      <UButton
        aria-label="Refresh issues"
        color="neutral"
        variant="outline"
        size="sm"
        :aria-busy="submitting"
        :disabled="submitting"
        @click="refreshInventory"
      >
        <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true" focusable="false">
          <path d="M20.5 12a8.5 8.5 0 1 1-2.5-6" /><path d="M20.5 3.5v5h-5" />
        </svg>
      </UButton>
    </div>

    <div class="mt-5 flex flex-wrap items-end gap-3">
      <div class="min-w-[220px] flex-1">
        <label for="filter-search" class="block text-xs text-muted">Find an issue</label>
        <UInput
          id="filter-search"
          v-model="search"
          placeholder="Title, repository or issue number"
          class="mt-1 w-full"
        />
      </div>
      <div class="min-w-[180px]">
        <span id="filter-repo-label" class="block text-xs text-muted">Repository</span>
        <USelectMenu
          v-model="repoValue"
          :items="repoOptions"
          value-key="value"
          aria-label="Repository"
          aria-labelledby="filter-repo-label"
          class="mt-1 w-full"
        >
          <template #trailing><SelectChevron /></template>
        </USelectMenu>
      </div>
      <div class="min-w-[160px]">
        <span id="filter-show-label" class="block text-xs text-muted">Show</span>
        <USelectMenu
          v-model="show"
          :items="SHOW_OPTIONS"
          value-key="value"
          aria-label="Show"
          aria-labelledby="filter-show-label"
          class="mt-1 w-full"
        >
          <template #trailing><SelectChevron /></template>
        </USelectMenu>
      </div>
    </div>

    <p id="freshness" role="status" class="mt-3 text-xs" :class="freshnessWarning ? 'text-amber-200' : 'text-muted'">{{ freshness }}</p>

    <UAlert v-if="fence" class="mt-3" role="status" color="warning" variant="subtle" :description="fence" />

    <template v-else>
      <section v-if="rows.length" class="mt-3 flex flex-col gap-3" aria-label="Issues">
        <IssueRow
          v-for="row in rows"
          :key="row.key"
          :row="row"
          :now="now"
          :pending="pendingAction(list?.actions, row.key)"
        />
      </section>
      <section v-else-if="empty" class="mt-3 rounded-xl border border-line bg-panel px-4 py-8 text-center">
        <h2 class="text-sm font-semibold text-white">{{ empty.title }}</h2>
        <p class="mt-1 text-sm text-muted">{{ empty.text }}</p>
        <UButton v-if="empty.link" :to="empty.link.to" size="sm" color="neutral" variant="outline" class="mt-3">{{ empty.link.label }}</UButton>
      </section>

      <div v-if="list" class="mt-3 flex flex-wrap items-center justify-between gap-3 text-xs text-muted">
        <p>{{ countText(list) }}</p>
        <div class="flex items-center gap-2">
          <UButton size="xs" color="neutral" variant="outline" :disabled="list.page <= 1" @click="goto(list.page - 1)">Previous</UButton>
          <UButton size="xs" color="neutral" variant="outline" :disabled="list.page >= Math.max(list.pages, 1)" @click="goto(list.page + 1)">Next</UButton>
        </div>
      </div>
    </template>
  </div>
</template>
