// Pure view logic and copy for the Issues list and the issue detail route.
// Every string the operator reads about an issue comes from one of the maps
// below, so the list, the detail and the decision dialog can never disagree.
// Rules follow the operator API contracts §2 (GET /api/issues) and §3.2–3.3.

import { elapsed } from './factory'
import { clampTitle, reviewerMentions } from './format'

export type IssueStatus = 'backlog' | 'queued' | 'working' | 'attention' | 'waiting' | 'completed'
export type IssueAction = 'queue' | 'backlog' | 'pause' | 'resume' | 'retry' | 'recheck' | 'assign_reviewer' | 'stop'

// Which pull request this is to the issue: the one Forge is carrying now, an
// earlier Forge draft it moved on from, or someone else's. Older payloads omit it.
export type PullRequestKind = 'candidate' | 'forge_attempt' | 'external'
export interface PullRequestRef { number: number; title: string; kind?: PullRequestKind }

export interface IssueScope { id: number; name: string; complete: boolean; fresh: boolean; at: number | null; error: string | null }
// `at` (epoch seconds): when the decision was recorded. The server already
// drops receipts older than the issue's latest event.
export interface IssueActionStatus { id: string; key: string; status: 'pending' | 'applied' | 'rejected'; message: string | null; at?: number }

// The plain-language card Forge keeps for every issue: one headline sentence
// and at most three sentences, written for a reader who does not read code.
export interface IssueSummary { headline: string; plain: string; role?: string; at?: number }

export interface IssueEnvironmentSelection { mode: 'automatic' | 'standard' | 'full'; services: string[] }
export interface IssueEnvironment {
  choice: { tier: 'standard' | 'full'; services: string[]; reason: string; automatic: boolean; evidence?: string[]; evidence_head?: string | null } | null
  override: IssueEnvironmentSelection | null
  full_available: boolean
  editable: boolean
}

export interface IssueRow {
  key: string; repo: string; repo_id: number; number: number; title: string
  content_hash: string; body_truncated: boolean; state: 'open' | 'closed'
  updated_at: string | null; labels: string[]; assignees: string[]
  status: IssueStatus; summary: IssueSummary | null; explanation: string
  // The server sends the decisions this issue accepts; 'stop' is never one of
  // them (it targets the job, not the issue) and is offered separately.
  actions: Exclude<IssueAction, 'stop'>[]
  setup_needed: boolean; environment_blocker: string | null
  decision_request?: { question: string; options: string[]; recommendation: string; evidence: string } | null
  environment?: IssueEnvironment
  pull_requests: PullRequestRef[]
  dependencies: number[]
  fresh: boolean; revision: string; setup_url: string; active: string | null
}

// Who was asked for the human approval, and when. Set by the assign_reviewer
// decision; the approval itself still has to come through GitHub.
export interface ReviewRequest { logins: string[]; at: number }
// Where Forge found this person: the repository's collaborators, the authors of
// recently merged work here, or the runtime's configured human reviewers.
export type ReviewerSource = 'collaborator' | 'recent_author' | 'configured'
export interface Reviewer { login: string; name: string; source?: ReviewerSource }

export interface IssueHistoryEvent { at: number; kind: string; reason?: string | null }
// `human_required`: the worker passed the change but says a person must still
// look (a secret, a migration, …); `human_reason` says why.
export interface IssueReview {
  role: 'review' | 'audit'; outcome: string | null; summary: string
  human_required?: boolean; human_reason?: string
}
export interface IssueAttempts { failed: number; limit: number }
// A GitHub label that paused the issue, who applied it and when (ISO time).
export interface IssueLabelHold { label: string; login: string | null; at: string }
export interface IssueDetail extends IssueRow {
  body: string; history: IssueHistoryEvent[]; reviews: IssueReview[]
  // Detail only: the list view carries neither the collaborators nor the request.
  reviewers: Reviewer[]; default_reviewers: string[]; reviewers_requested: ReviewRequest | null
  attempts?: IssueAttempts | null
  held_by?: IssueLabelHold[] | null
}

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
  assign_reviewer: 'Assign reviewer',
  stop: 'Stop attempt',
}

export const ACTION_EFFECTS: Record<IssueAction, string> = {
  queue: 'Forge will check repository setup, dependencies and existing work before starting when capacity is available.',
  backlog: 'This removes the issue from Forge’s queue. The GitHub issue stays open.',
  pause: 'Forge will leave this issue on hold until you choose to resume it.',
  resume: 'This releases the deliberate hold. Confirm that the prerequisite work is complete. Forge will check dependencies and existing work before continuing.',
  retry: 'This grants a new three-attempt budget and returns the issue to Build. Existing history is preserved.',
  recheck: 'Forge will recheck readiness and resume the held stage. The existing pull request and failure budget are preserved. Confirm that the reported blocker has been addressed.',
  assign_reviewer: 'Forge asks GitHub for a review from these people and assigns them to the issue. The approval still has to come from a reviewer with write access who did not write the code.',
  stop: 'This stops the active attempt and discards its unfinished workspace changes. Published pull requests are preserved.',
}

export const EVENT_LABELS: Record<string, string> = {
  discovered: 'Added to Forge',
  held: 'Put on hold',
  protection_paused: 'Repository setup paused work',
  protection_hold_migrated: 'Recognized an earlier setup hold',
  protection_restored: 'Repository setup verified',
  human_retry: 'Another attempt approved',
  draft_resumed: 'Continuing an earlier draft',
  decision_requested: 'Waiting for your decision',
  decision_answered: 'Your answer recorded',
  environment_selected: 'Worker environment selected',
  environment_override: 'Issue environment preference saved',
  services_retry: 'Retrying with database services',
  operator_queue: 'Queued for Forge',
  operator_backlog: 'Returned to backlog',
  operator_pause: 'Paused by an operator',
  operator_resume: 'Hold released',
  operator_recheck: 'Repository setup checked',
  reviewer_requested: 'Review requested',
  operator_conflict: 'Decision needs another review',
  operator_synced: 'GitHub labels confirmed',
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

// What the issue page leads with: the headline, then the description, then the
// action bar. Everything the worker wrote stays available under "Details", so
// the reader never has to open it to learn where the issue stands or why.
export interface SummaryBlock { headline: string; plain: string; details: string }

export function summaryBlock(
  issue: Pick<IssueDetail, 'summary' | 'explanation' | 'environment_blocker'> | null | undefined,
): SummaryBlock | null {
  if (!issue) return null
  const details = [issue.explanation, issue.environment_blocker].map(part => (part ?? '').trim()).filter(Boolean).join(' ')
  const headline = (issue.summary?.headline ?? '').trim() || leadSentence(details)
  const plain = (issue.summary?.plain ?? '').trim() || (headline === details ? '' : details)
  if (!headline && !plain) return null
  return { headline, plain, details }
}

// Cards and list rows carry the headline when Forge has one; the older
// explanation line is the fallback, never both.
export function rowNote(row: Pick<IssueRow, 'summary' | 'explanation'>, max = 96): string {
  return clampTitle(((row.summary?.headline ?? '').trim() || (row.explanation ?? '').trim()), max)
}

// A review summary is worker prose: the detail page shows its lead sentence
// clamped to two lines, and reveals the rest on demand. The row model is pure
// so the copy the operator reads is decided here, not in the template.
export type ReviewOutcome = 'passed' | 'rejected' | 'unable' | 'unknown'

export interface ReviewRow {
  role: IssueReview['role']
  label: string
  // The verdict this reviewer returned, never the issue's own status: a passed
  // review on an issue that needs attention is still a passed review.
  outcome: ReviewOutcome
  outcomeLabel: string
  // A pass that still needs a person reads as a warning, not a green light.
  tone: 'success' | 'error' | 'warning' | 'neutral'
  lead: string
  summary: string
  // True when the lead leaves something out, so the row offers "More".
  more: boolean
}

const REVIEW_LABELS: Record<IssueReview['role'], string> = { review: 'Review', audit: 'Security review' }
// Worker verdicts ('pass'/'reject'/'unable'), plus the older published names.
const REVIEW_OUTCOMES: Record<string, ReviewOutcome> = {
  pass: 'passed', passed: 'passed', approved: 'passed',
  reject: 'rejected', rejected: 'rejected', changes_requested: 'rejected',
  unable: 'unable',
}
const REVIEW_OUTCOME_LABELS: Record<ReviewOutcome, string> = {
  passed: 'Passed', rejected: 'Rejected', unable: 'Could not review', unknown: 'No verdict',
}
const REVIEW_TONES: Record<ReviewOutcome, ReviewRow['tone']> = {
  passed: 'success', rejected: 'error', unable: 'warning', unknown: 'neutral',
}

function leadSentence(text: string): string {
  const end = text.search(/[.!?](\s|$)/)
  return end === -1 ? text : text.slice(0, end + 1)
}

export function reviewRow(review: IssueReview): ReviewRow {
  const outcome = REVIEW_OUTCOMES[(review.outcome ?? '').toLowerCase()] ?? 'unknown'
  // The summary of such a pass is often just "PASS."; the reason is the news.
  const needsPerson = outcome === 'passed' && !!review.human_required
  const text = (needsPerson && (review.human_reason ?? '').trim()) || review.summary || ''
  const summary = text.replace(/\s+/g, ' ').trim()
  const lead = leadSentence(summary)
  return {
    role: review.role,
    label: REVIEW_LABELS[review.role] ?? review.role,
    outcome,
    outcomeLabel: needsPerson ? 'Passed · needs a person' : REVIEW_OUTCOME_LABELS[outcome],
    tone: needsPerson ? 'warning' : REVIEW_TONES[outcome],
    lead,
    summary,
    more: summary !== lead,
  }
}

export function reviewRows(reviews: IssueReview[] | null | undefined): ReviewRow[] {
  return (reviews ?? []).map(reviewRow)
}

// The reviewer control on an approval_needed issue. Everything the operator
// reads or sends is decided here: the select's options, what it starts with,
// the decision text that becomes `reason`, and the line the page shows once
// GitHub has been asked.
export interface ReviewerOption { value: string; label: string; description?: string }

const REVIEWER_SOURCES: Record<ReviewerSource, string> = {
  collaborator: 'collaborator',
  recent_author: 'recently merged work here',
  configured: 'configured reviewer',
}

// Only people Forge found for this repository: there is no free-text entry,
// so a typo can never ask a stranger for the review.
export function reviewerOptions(reviewers: Reviewer[] | null | undefined): ReviewerOption[] {
  return (reviewers ?? [])
    .filter(person => !!person?.login)
    .map((person) => {
      const option: ReviewerOption = { value: person.login, label: person.name ? `${person.login} · ${person.name}` : person.login }
      const source = person.source ? REVIEWER_SOURCES[person.source] : undefined
      if (source) option.description = source
      return option
    })
}

// Pre-selected: the people the runtime already trusts to approve, kept to the
// ones this repository actually offers so the select never holds a stranger.
export function preselectedReviewers(
  detail: Pick<IssueDetail, 'reviewers' | 'default_reviewers'> | null | undefined,
): string[] {
  const known = new Set((detail?.reviewers ?? []).map(person => person.login.toLowerCase()))
  return (detail?.default_reviewers ?? []).filter(login => known.has(login.toLowerCase()))
}

export function reviewerReason(logins: string[]): string {
  const cleaned = logins.map(login => login.trim().replace(/^@+/, '')).filter(Boolean)
  return [...new Set(cleaned)].join(', ')
}

export function reviewRequestText(request: ReviewRequest | null | undefined, now: number): string {
  if (!request?.logins?.length) return ''
  const line = `Review requested from ${reviewerMentions(request.logins)}`
  const age = now - (request.at ?? 0)
  return request.at && age >= 0 ? `${line} · ${elapsed(age)} ago` : line
}

// The pull request a review is requested on: the candidate Forge carries,
// else (an older payload without kinds) the first one listed.
export function reviewPull(pulls: PullRequestRef[] | null | undefined): PullRequestRef | null {
  const list = pulls ?? []
  return list.find(pull => pull.kind === 'candidate') ?? list.find(pull => !pull.kind) ?? null
}

const PULL_PREFIXES: Record<PullRequestKind, string> = {
  candidate: 'PR', forge_attempt: 'Earlier Forge draft', external: 'Other PR',
}

export function pullLabel(pull: PullRequestRef): string {
  return `${(pull.kind && PULL_PREFIXES[pull.kind]) || 'PR'} #${pull.number}`
}

export function attemptsText(attempts: IssueAttempts | null | undefined): string {
  if (!attempts || !(attempts.failed > 0)) return ''
  return `${attempts.failed} of ${attempts.limit} attempts failed`
}

// "Labelled forge:hold by @maintainer on Sep 21", one line per label. The
// date is the UTC day, so the server and the browser render the same text.
export function heldByLines(holds: IssueLabelHold[] | null | undefined): string[] {
  return (holds ?? []).filter(hold => !!hold?.label).map((hold) => {
    const by = hold.login ? ` by @${hold.login}` : ''
    const at = new Date(hold.at)
    const on = Number.isNaN(at.getTime())
      ? ''
      : ` on ${at.toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' })}`
    return `Labelled ${hold.label}${by}${on}`
  })
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
