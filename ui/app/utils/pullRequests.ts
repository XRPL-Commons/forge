import type { JobView } from './factory'

export interface PullRequestWork {
  key: string; repo_id: number; repo: string; number: number; title: string; url: string
  unit_id: string; candidate: { id: string; head: string; base: string; base_ref: string; pr: number } | null
  stage_code: string; state: string; status: 'queued' | 'running' | 'deferred' | 'blocked' | 'completed'
  reason: string | null; reason_code: string | null; retry_at: number | null
  active_job_id: string | null; updated_at: number; created_at: number; enabled: boolean
}

export interface PullRequestDetail {
  selected_job?: JobView | null
  ok: true; pull_request: PullRequestWork; jobs: JobView[]; jobs_next: string | null
  gates: Array<{ code: string; reason: string }>; observed_at: number
}

export interface PullRequestList {
  ok: true; pull_requests: PullRequestWork[]; next: string | null; observed_at: number
}

export function pullRequestPath(pull: Pick<PullRequestWork, 'repo_id' | 'number'>): string {
  return `/pull-requests/${pull.repo_id}/${pull.number}`
}

export function pullRequestStage(stage: string): string {
  return ({ reviewing: 'Review', auditing: 'Audit', merging: 'Merge checks', merged: 'Merged', closed: 'Closed' } as Record<string, string>)[stage] ?? stage.replace(/_/g, ' ')
}

export function pullRequestStatus(status: PullRequestWork['status']): string {
  return { queued: 'Queued', running: 'Running', deferred: 'Waiting to retry', blocked: 'Blocked', completed: 'Completed' }[status]
}

/** Only a job pinned to the current candidate may be presented as current work. */
export function currentPullJob(job: JobView, pull: PullRequestWork): boolean {
  return !!pull.candidate && job.candidate_id === pull.candidate.id
}

export function mergePullJobs(recent: JobView[], older: JobView[]): JobView[] {
  const jobs = new Map(older.map(job => [job.id, job]))
  for (const job of recent) jobs.set(job.id, job)
  return [...jobs.values()].sort((a, b) => b.created_at - a.created_at || a.id.localeCompare(b.id))
}
