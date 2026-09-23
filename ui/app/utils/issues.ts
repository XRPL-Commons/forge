// Pure view logic and copy for the Issues list and the issue detail route.
// Every string the operator reads about an issue comes from one of the maps
// below, so the list, the detail and the decision dialog can never disagree.
// Rules follow the operator API contracts §2 (GET /api/issues) and §3.2–3.3.

export type IssueStatus = 'backlog' | 'queued' | 'working' | 'attention' | 'waiting' | 'completed'
export type IssueAction = 'queue' | 'backlog' | 'pause' | 'resume' | 'retry' | 'recheck' | 'stop'

export interface IssueScope { id: number; name: string; complete: boolean; fresh: boolean; at: number | null; error: string | null }
export interface IssueActionStatus { id: string; key: string; status: 'pending' | 'applied' | 'rejected'; message: string | null }

export interface IssueRow {
  key: string; repo: string; repo_id: number; number: number; title: string
  content_hash: string; body_truncated: boolean; state: 'open' | 'closed'
  updated_at: string | null; labels: string[]; assignees: string[]
  status: IssueStatus; explanation: string
  // The server sends the decisions this issue accepts; 'stop' is never one of
  // them (it targets the job, not the issue) and is offered separately.
  actions: Exclude<IssueAction, 'stop'>[]
  setup_needed: boolean; environment_blocker: string | null
  pull_requests: { number: number; title: string }[]
  dependencies: number[]
  fresh: boolean; revision: string; setup_url: string; active: string | null
}

export interface IssueHistoryEvent { at: number; kind: string; reason?: string | null }
export interface IssueReview { role: 'review' | 'audit'; outcome: string | null; summary: string }
export interface IssueDetail extends IssueRow { body: string; history: IssueHistoryEvent[]; reviews: IssueReview[] }

export interface IssueListResponse {
  ok: true; controls_token: string; issues: IssueRow[]; total: number; page: number; pages: number
  repositories: IssueScope[]; complete: boolean; actions: IssueActionStatus[]
}
export interface IssueDetailResponse {
  ok: true; controls_token: string; issue: IssueDetail; repositories: IssueScope[]; actions: IssueActionStatus[]
}

export const STATUS_NAMES: Record<IssueStatus, string> = {
  backlog: 'Backlog',
  queued: 'Queued',
  working: 'Working',
  attention: 'Needs attention',
  completed: 'Completed',
  waiting: 'Waiting',
}

export const ACTION_LABELS: Record<IssueAction, string> = {
  queue: 'Queue for Forge',
  backlog: 'Return to backlog',
  pause: 'Pause',
  resume: 'Release hold',
  retry: 'Retry attempt',
  recheck: 'Recheck readiness',
  stop: 'Stop attempt',
}

export const ACTION_EFFECTS: Record<IssueAction, string> = {
  queue: 'Forge will check repository setup, dependencies and existing work before starting when capacity is available.',
  backlog: 'This removes the issue from Forge’s queue. The GitHub issue stays open.',
  pause: 'Forge will leave this issue on hold until you choose to resume it.',
  resume: 'This releases the deliberate hold. Confirm that the prerequisite work is complete. Forge will check dependencies and existing work before continuing.',
  retry: 'This grants a new three-attempt budget and returns the issue to Build. Existing history is preserved.',
  recheck: 'Forge will recheck readiness and resume the held stage. The existing pull request and failure budget are preserved. Confirm that the reported blocker has been addressed.',
  stop: 'This stops the active attempt and discards its unfinished workspace changes. Published pull requests are preserved.',
}

export const EVENT_LABELS: Record<string, string> = {
  discovered: 'Added to Forge',
  held: 'Put on hold',
  protection_paused: 'Repository setup paused work',
  protection_hold_migrated: 'Recognized an earlier setup hold',
  protection_restored: 'Repository setup verified',
  human_retry: 'Another attempt approved',
  operator_queue: 'Queued for Forge',
  operator_backlog: 'Returned to backlog',
  operator_pause: 'Paused by an operator',
  operator_resume: 'Hold released',
  operator_recheck: 'Repository setup checked',
  operator_conflict: 'Decision needs another review',
  reserved: 'Attempt prepared',
  merged: 'Pull request merged',
  cancelled: 'Attempt stopped',
}

export const SHOW_OPTIONS: { value: string; label: string }[] = [
  { value: 'open', label: 'Open issues' },
  { value: 'attention', label: 'Needs attention' },
  { value: 'backlog', label: 'Backlog' },
  { value: 'queued', label: 'Queued' },
  { value: 'working', label: 'Working' },
  { value: 'waiting', label: 'Waiting' },
  { value: 'completed', label: 'Completed' },
]

// Decisions that change what Forge does next are recorded with the operator's
// own words; the reversible ones default to "<label> confirmed by operator".
const REASON_REQUIRED: IssueAction[] = ['pause', 'resume', 'retry', 'stop']

export function decisionNeedsReason(action: IssueAction): boolean {
  return REASON_REQUIRED.includes(action)
}

export function defaultReason(action: IssueAction): string {
  return `${ACTION_LABELS[action]} confirmed by operator`
}

// A review summary is worker prose: the detail page shows its lead sentence
// clamped to two lines, and reveals the rest on demand. The row model is pure
// so the copy the operator reads is decided here, not in the template.
export interface ReviewRow {
  role: IssueReview['role']
  label: string
  outcome: 'passed' | 'attention'
  outcomeLabel: string
  lead: string
  summary: string
  // True when the lead leaves something out, so the row offers "More".
  more: boolean
}

const REVIEW_LABELS: Record<IssueReview['role'], string> = { review: 'Review', audit: 'Security review' }

function leadSentence(text: string): string {
  const end = text.search(/[.!?](\s|$)/)
  return end === -1 ? text : text.slice(0, end + 1)
}

export function reviewRow(review: IssueReview): ReviewRow {
  const summary = (review.summary ?? '').replace(/\s+/g, ' ').trim()
  const lead = leadSentence(summary)
  const passed = review.outcome === 'passed'
  return {
    role: review.role,
    label: REVIEW_LABELS[review.role] ?? review.role,
    outcome: passed ? 'passed' : 'attention',
    outcomeLabel: passed ? 'Passed' : 'Needs attention',
    lead,
    summary,
    more: summary !== lead,
  }
}

export function reviewRows(reviews: IssueReview[] | null | undefined): ReviewRow[] {
  return (reviews ?? []).map(reviewRow)
}

export function eventLabel(kind: string): string {
  return EVENT_LABELS[kind] ?? kind.replace(/_/g, ' ')
}

export function issuePath(key: string, job?: string | null): string {
  return `/issues/${encodeURIComponent(key)}${job ? `?job=${encodeURIComponent(job)}` : ''}`
}

export interface ListFilters { state: string; repoId: number | null; q: string; page: number }

export function listParams({ state, repoId, q, page }: ListFilters): string {
  const params = new URLSearchParams()
  params.set('state', state)
  params.set('page', String(page))
  if (repoId !== null && repoId !== undefined) params.set('repo_id', String(repoId))
  const search = (q ?? '').trim()
  if (search) params.set('q', search)
  return params.toString()
}

// The last decision recorded for a key is the one being applied: the server
// appends a new IssueActionStatus per request and keeps the previous ones.
export function pendingAction(actions: IssueActionStatus[] | undefined, key: string): IssueActionStatus | null {
  const forKey = (actions ?? []).filter(action => action.key === key)
  const last = forKey[forKey.length - 1]
  return last && last.status === 'pending' ? last : null
}

export function lastAction(actions: IssueActionStatus[] | undefined, key: string): IssueActionStatus | null {
  const forKey = (actions ?? []).filter(action => action.key === key)
  return forKey[forKey.length - 1] ?? null
}

export function rowAction(row: Pick<IssueRow, 'status'>, pending: IssueActionStatus | null | undefined): string {
  if (pending) return 'Applying…'
  if (row.status === 'working') return 'View progress'
  if (row.status === 'attention') return 'Review'
  return 'View issue'
}

export function freshnessText(list: Pick<IssueListResponse, 'repositories' | 'complete'>): string {
  const failing = (list.repositories ?? []).find(scope => scope.error)
  if (failing?.error) return failing.error
  if (!(list.repositories ?? []).length) return ''
  if (list.complete) return 'Up to date · Viewing issues never queues them for work.'
  return 'Showing received issues. The GitHub inventory is still incomplete or needs refreshing.'
}

export interface EmptyState { title: string; text: string; link?: { to: string; label: string } }

export function emptyState(list: Pick<IssueListResponse, 'issues' | 'repositories' | 'complete'>): EmptyState | null {
  if ((list.issues ?? []).length) return null
  if (!(list.repositories ?? []).length) {
    return {
      title: 'Choose where Forge should work',
      text: 'Add and enable a repository to see its issues here.',
      link: { to: '/settings/repository', label: 'Add repository →' },
    }
  }
  if (!list.complete) {
    return { title: 'Loading the issue list', text: 'Previously received issues remain visible while Forge checks GitHub.' }
  }
  return { title: 'No issues in this view', text: 'Try another filter.' }
}

export function countText(list: Pick<IssueListResponse, 'total' | 'page' | 'pages' | 'complete'>): string {
  const issues = `${list.total} ${list.total === 1 ? 'issue' : 'issues'}${list.complete ? '' : ' received'}`
  return `${issues} · Page ${list.page} of ${Math.max(list.pages, 1)}`
}
