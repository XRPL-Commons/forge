<script setup lang="ts">
import { jobSummary, type JobView } from '~/utils/factory'
import { emptyOutputText, outputText, type OutputView } from '~/utils/liveWork'

// The selected run's output and every run this issue has had. IssueDetail
// owns the request (the bubble reads the same payload for the live activity),
// so this only renders it: the text without heartbeats (or raw on demand),
// following the end of a live tail unless the operator scrolled up to read.
const props = defineProps<{
  jobs: JobView[]
  selectedJob: string | null
  output: OutputView | null
  error?: string | null
  // When the output was last read (ms), for "updated 3s ago".
  updatedAt?: number
}>()
const open = defineModel<boolean>('open', { default: false })
const route = useRoute()

const raw = ref(false)
const text = computed(() => outputText(props.output, raw.value))
// Raw only differs when the tail carries heartbeats; otherwise no toggle.
const hasHidden = computed(() => !!props.output && outputText(props.output, true) !== outputText(props.output))
const live = computed(() => !!props.output?.live && !props.output?.unavailable)

const nowMs = ref(0)
let ticker: ReturnType<typeof setInterval> | undefined
onMounted(() => {
  nowMs.value = Date.now()
  ticker = setInterval(() => { if (!document.hidden) nowMs.value = Date.now() }, 1000)
})
onBeforeUnmount(() => clearInterval(ticker))
const updated = computed(() => {
  if (!props.updatedAt || !nowMs.value) return ''
  return `updated ${Math.max(0, Math.round((nowMs.value - props.updatedAt) / 1000))}s ago`
})

// Follow the tail unless the operator scrolled away from the end.
const box = ref<HTMLElement | null>(null)
const following = ref(true)
function onScroll() {
  const el = box.value
  if (el) following.value = el.scrollHeight - el.scrollTop - el.clientHeight < 24
}
watch([text, open], async () => {
  if (!following.value) return
  await nextTick()
  if (box.value) box.value.scrollTop = box.value.scrollHeight
}, { flush: 'post' })
watch(() => props.selectedJob, () => { following.value = true; raw.value = false })

// Times are formatted only after mount: the server has no idea which time
// zone the operator is in, and a hydration mismatch is not worth a second.
const mounted = ref(false)
onMounted(() => { mounted.value = true })
function localTime(at: number): string {
  const iso = new Date(at * 1000).toISOString()
  return mounted.value ? new Date(at * 1000).toLocaleString() : iso
}
function isoTime(at: number): string { return new Date(at * 1000).toISOString() }
</script>

<template>
  <section id="issue-output" class="border-t border-line py-1">
    <UCollapsible v-model:open="open">
      <UButton color="neutral" variant="ghost" size="sm" class="w-full justify-between px-1 text-muted">
        <span class="flex items-center gap-2">
          Worker output
          <span v-if="live" id="issue-output-live" class="inline-flex items-center gap-1.5 text-xs font-normal text-mint-200">
            <span aria-hidden="true" class="size-1.5 rounded-full bg-mint-400 motion-safe:animate-pulse" />
            live<template v-if="updated"> · {{ updated }}</template>
          </span>
        </span>
        <UIcon name="i-lucide-chevron-down" class="size-4 transition-transform" :class="open ? 'rotate-180' : ''" />
      </UButton>
      <template #content>
        <p v-if="error" role="status" class="mt-2 text-xs text-amber-200">{{ error }}</p>
        <template v-if="text.trim()">
          <div v-if="hasHidden" class="mt-1 flex justify-end">
            <UButton
              id="issue-output-raw"
              size="xs"
              color="neutral"
              variant="link"
              :aria-pressed="raw"
              @click="raw = !raw"
            >{{ raw ? 'Hide heartbeats' : 'Show raw' }}</UButton>
          </div>
          <pre
            ref="box"
            class="mt-1 max-h-96 overflow-auto whitespace-pre-wrap break-words rounded-lg border border-line bg-panel p-3 font-mono text-xs leading-relaxed"
            @scroll.passive="onScroll"
          >{{ text }}</pre>
        </template>
        <p v-else id="issue-output-empty" class="mt-2 text-xs text-muted">{{ emptyOutputText(output) }}</p>
      </template>
    </UCollapsible>
    <ul v-if="jobs.length" id="issue-jobs" class="mt-1 mb-2 flex flex-col gap-1 px-1 text-xs">
      <li v-for="job in jobs" :key="job.id">
        <NuxtLink
          :to="{ query: { ...route.query, job: job.id } }"
          :aria-current="job.id === selectedJob ? 'true' : undefined"
          class="hover:underline"
          :class="job.id === selectedJob ? 'text-mint-200' : 'text-muted'"
        >{{ jobSummary(job) }}<span v-if="job.started_at"> · <time :datetime="isoTime(job.started_at)">{{ localTime(job.started_at) }}</time></span></NuxtLink>
      </li>
    </ul>
  </section>
</template>
