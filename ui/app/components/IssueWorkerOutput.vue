<script setup lang="ts">
import { jobSummary, type JobView } from '~/utils/factory'

// The selected run's captured output and every run this issue has had. It
// lives in the issue's Details tab, and on its own when only a run (and not
// its issue) is reachable: see IssueDetail's showOutput.
const props = defineProps<{ jobs: JobView[]; selectedJob: string | null }>()
const open = defineModel<boolean>('open', { default: false })
const api = useForgeApi()
const route = useRoute()

const output = useResource<{ ok: true; logs: string }>(
  () => `/api/factory/output?id=${encodeURIComponent(props.selectedJob ?? '')}`,
  path => api.get(path, { timeout: 15000 }),
  { allowed: () => open.value && !!props.selectedJob, server: false },
)
onMounted(output.start)
const logs = computed(() => output.data.value?.logs ?? null)

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
  <section class="border-t border-line py-1">
    <UCollapsible v-model:open="open">
      <UButton color="neutral" variant="ghost" size="sm" class="w-full justify-between px-1 text-muted">
        Worker output
        <UIcon name="i-lucide-chevron-down" class="size-4 transition-transform" :class="open ? 'rotate-180' : ''" />
      </UButton>
      <template #content>
        <p v-if="output.error.value" role="status" class="mt-2 text-xs text-amber-200">{{ output.error.value }}</p>
        <pre
          v-if="logs && logs.trim()"
          class="mt-2 max-h-96 overflow-auto whitespace-pre-wrap rounded-lg border border-line bg-panel p-3 text-xs"
        >{{ logs }}</pre>
        <p v-else class="mt-2 text-xs text-muted">No captured output is available yet.</p>
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
