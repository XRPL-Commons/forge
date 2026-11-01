import type { JobView } from '~/utils/factory'
import type { OutputView } from '~/utils/liveWork'
import { currentPullJob, mergePullJobs, type PullRequestDetail } from '~/utils/pullRequests'

export function usePullRequest() {
  const route = useRoute()
  const api = useForgeApi()
  const requestedJob = computed(() => typeof route.query.job === 'string' ? route.query.job : null)
  const path = computed(() => `/api/pull-requests?repo_id=${encodeURIComponent(String(route.params.repoId))}&number=${encodeURIComponent(String(route.params.number))}`)
  const detailPath = computed(() => path.value + (requestedJob.value ? `&job=${encodeURIComponent(requestedJob.value)}` : ''))
  const poll = useResource<PullRequestDetail>(detailPath, key => api.get(key))
  const older = ref<JobView[]>([])
  const olderNext = ref<string | null | undefined>()
  const loadingOlder = ref(false)
  const historyError = ref('')
  const pull = computed(() => poll.data.value?.pull_request ?? null)
  const jobs = computed(() => mergePullJobs(poll.data.value?.jobs ?? [], [
    ...(poll.data.value?.selected_job ? [poll.data.value.selected_job] : []), ...older.value,
  ]))
  const selectedJob = computed(() => {
    if (requestedJob.value) return jobs.value.find(job => job.id === requestedJob.value)?.id ?? null
    return pull.value?.active_job_id ?? jobs.value.find(job => pull.value && currentPullJob(job, pull.value))?.id ?? null
  })
  const selected = computed(() => jobs.value.find(job => job.id === selectedJob.value) ?? null)
  const outputOpen = ref(false)
  watch(requestedJob, job => { if (job) outputOpen.value = true }, { immediate: true })
  const output = useResource<OutputView>(
    () => `/api/factory/output?id=${encodeURIComponent(selectedJob.value ?? '')}`,
    key => api.get(key), { server: false, allowed: () => outputOpen.value && !!selected.value },
  )
  const nextHistory = computed(() => olderNext.value === undefined ? poll.data.value?.jobs_next : olderNext.value)
  watch(path, () => { older.value = []; olderNext.value = undefined; historyError.value = ''; outputOpen.value = false })
  async function loadOlder() {
    const cursor = nextHistory.value
    if (!cursor || loadingOlder.value) return
    loadingOlder.value = true
    historyError.value = ''
    const requestPath = path.value
    try {
      const page = await api.get<PullRequestDetail>(`${requestPath}&before=${encodeURIComponent(cursor)}`)
      if (requestPath !== path.value) return
      older.value = mergePullJobs(older.value, page.jobs)
      olderNext.value = page.jobs_next
    } catch (error) {
      historyError.value = error instanceof Error ? error.message : 'Could not load earlier attempts.'
    } finally { loadingOlder.value = false }
  }
  onMounted(() => { poll.start(); output.start() })
  return { poll, pull, jobs, selectedJob, selected, output, outputOpen, nextHistory, loadingOlder, historyError, loadOlder }
}
