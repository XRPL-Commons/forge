// Pure view logic for the Activity (Factory) page: every card-shaped surface
// renders a CardModel, every banner is produced by warnings(), so no component
// reaches into the raw FactoryView payload. Rules follow the operator API
// contracts §2 (GET /api/factory) and §3.1 (Activity page).
import { operatorLabel, operatorReason, type OperatorStatusSource } from './operatorStatus'
import type { CodingAccount } from './accounts'
import { clampTitle, reviewerMentions, spelledAgo } from './format'
import type { PortraitLook } from './cube'
import { isDropped, ledgerLook, type ClosedAs } from './workbench'
import type { OnboardingView } from './onboarding'
import type { IssueSummary } from './issues'
import type { DeploymentRow, FactoryDeployment } from './repositories'

export type JobRole = 'build' | 'review' | 'audit' | 'recovery' | 'merge'
export type ColumnRole = 'build' | 'review' | 'audit'
export type IssueState = 'ready' | 'built' | 'reviewed' | 'audited' | 'approval_needed'
  | 'investigating' | 'hold' | 'failed' | 'merged' | 'cancelled'

export interface ResourceUsage {
  captured_at: number | null
  wall: { seconds: number | null; started_at: number | null; finished_at: number | null; provenance: 'docker_inspect'; unavailable_reason: string | null }
  sample: { sampled_at: number; cpu_seconds: number | null; memory_bytes: number | null; provenance: 'docker_engine_stats' } | null
  collection: { checked_at: number | null; status: 'available' | 'partial' | 'unavailable'; reason: string | null }
}

export interface MirrorReceipt {
  id: string; created_at: number; repository?: string; repository_id?: number
  request_kind?: string; outcome?: string; reason?: string; phase?: string
  cache?: boolean; host_fetch?: boolean; request_bytes?: number; response_bytes?: number
}
export interface MirrorEvidence {
  receipts: MirrorReceipt[]; claims: MirrorReceipt[]
  receipts_total: number; claims_total: number; receipts_truncated: boolean; claims_truncated: boolean
}
export interface JobView {
  publication?: { intent_id: string | null; status: string; updated_at: number; claim: { code?: string; reason?: string; paths?: string[] } | null; active: boolean } | null
  mirror?: MirrorEvidence
  id: string; issue_key: string | null; repo_id: number; repo: string; issue_number: number | null
  unit_id?: string; members?: Array<{ member_id: string; issue_key: string; issue_number: number }>
  builder_id?: string | null; builder_stage?: 'planning' | 'integration' | 'child' | 'task' | null
  parent_job_id?: string | null; delegation_key?: string | null
  resource_usage?: ResourceUsage
  task_id?: string | null; task_key?: string | null; task_executor?: 'owner' | 'child' | null
  role: JobRole; slot: string; provider: string; provider_id: string | null; model: string | null
  phase: 'reserved' | 'created' | 'launching' | 'running' | 'stopping' | 'cleaned' | 'external'
  outcome: string | null; cleanup: boolean; fenced: boolean
  capacity_wait_since?: number | null; created_at: number; deadline: number
  started_at: number | null; observed_status?: string | null; observed_at: number | null; last_observed?: number
  // The branch the worker pushes to; older payloads omit it.
  branch?: string | null
}

export function jobIncludesIssue(job: JobView, issueKey: string): boolean {
  return job.members ? job.members.some(member => member.issue_key === issueKey) : job.issue_key === issueKey
}

interface LedgerEvent { kind: string; provider?: string; model?: string | null; [key: string]: unknown }

export interface LedgerIssueView extends OperatorStatusSource {
  key: string; repo_id: number; repo: string; number: number; title: string
  attempts?: Array<Pick<JobView, 'id' | 'role' | 'outcome' | 'phase' | 'created_at' | 'started_at'>>
  state: IssueState; reason: string | null; active: string | null; failure_count: number
  hold_kind?: string | null
  // A transitional gate (#241): the stage is settling the job that just
  // finished, e.g. checking a review's result. Not a hold. Older payloads omit it.
  transition?: { code: string; role: string | null; job_id: string | null } | null
  merge_gate_kind?: string | null
  candidate?: { pr?: number } | null
  // The plain-language card of the last outcome (contracts §2, LedgerIssueView).
  last_summary?: IssueSummary | null
  reviewers_requested?: { logins: string[]; at: number } | null
  prerequisite_hold?: { state: string } | true | null
  // Open issues this one depends on; the supervisor never dispatches it meanwhile.
  blocked_dependencies?: number[]
  dependency_reason?: string | null
  // GitHub's reason the issue closed; not_planned and duplicate read as
  // "No longer needed" (workbench.ts isDropped). Older payloads omit it.
  closed_as?: ClosedAs | null
  ownership_wait?: IssueSummary | null
  // Why the issue waits for the operator (control.py attention_kind), null
  // when it does not. Older payloads omit it.
  attention_kind?: 'approve' | 'question' | 'approach' | 'failed' | 'env' | 'review' | 'verify' | null
  history?: LedgerEvent[]
  // Deployment verdicts (contracts §2). Both are present and null until the
  // supervisor has evidence; `deployed` is written once, `deploy_failed` is
  // cleared when the merge finally reaches production.
  deployed?: { commit: string; at: number; via: 'live' | 'github' } | null
  deploy_failed?: { sha: string; at: number; run_url: string | null } | null
  // Only on a paged `section=completed` read: when it was merged or closed.
  completed_at?: number | null
}

interface ObservationError { attempt?: string; issue?: string; phase: string; reason: string }
interface OperatorStatus { title: string; detail: string; action_url: string | null; action_label: string | null; empty?: true }
interface OperatorRequest { id: string; action: string; target: string; reason: string; status: 'pending' | 'applied' | 'rejected'; error?: string }

interface FactorySummary {
  completed_count: number; runner_count: number; runners_running: boolean
  workers: Pick<JobView, 'id' | 'slot' | 'role' | 'phase' | 'cleanup' | 'fenced' | 'outcome' | 'observed_at' | 'observed_status' | 'last_observed'>[]
  merged_day: number; merged_week: number; merged_total: number
  deployed_day: number; waiting: number; deploy_failed: number
}
export interface FactoryView {
  cursor?: string
  summary?: FactorySummary
  ok: true; controls_token: string; active: boolean
  operator: OperatorStatus | null
  repositories?: { id: number; name: string; enabled: boolean; deployment?: DeploymentRow }[]
  observation?: { at: number; errors?: ObservationError[] } | Record<string, never>
  actions?: OperatorRequest[]
  issues?: LedgerIssueView[]
  jobs?: JobView[]
  slots?: Record<string, string>
  // One entry per repository the supervisor has observed, keyed by id as a
  // string (contracts §2).
  deployments?: Record<string, FactoryDeployment>
  runtime: { enabled: boolean; slots: Record<string, number>; cycle_seconds: number; [key: string]: unknown }
  // The digest CapacityForm sends back on POST /api/runtime (contracts §2).
  runtime_revision: string
  // One row per coding account in the pool (utils/accounts.ts).
  coding_accounts?: CodingAccount[]
}

export interface CardModel extends OperatorStatusSource {
  key: string
  // Anchor target and FLIP key: "issue-<repo_id>-<number>", or "job-<id>" for
  // an internal row, whose issue key may already belong to a rendered issue.
  id: string
  to: string
  repo: string
  number: number | null
  title: string
  phaseLabel: string
  providerLine: string
  reason: string
  timerStart: number | null
  timerEnd: number | null
  running: boolean
  stoppable: boolean
  jobId: string | null
  footerLink: { to: string; label: string } | null
  // "deployed" / "waiting for deploy" / "deploy failed" on a completed card,
  // '' when this repository's deployments are not tracked.
  deployment: string
  // What the card's cube portrait shows, and the facts its meta line reads.
  // `state` is null for an internal job, which has no issue row.
  state: IssueState | null
  look: PortraitLook
  summary: string
  pr: number | null
  failures: number
  // Unix seconds: the last thing the ledger recorded, and when the issue was
  // merged or closed (null while it is open).
  updatedAt: number | null
  doneAt: number | null
  // At a machine without a live job because that stage is settling its result.
  checking?: boolean
}


export const NEXT_ROLE: Record<string, ColumnRole> = { ready: 'build', built: 'review', reviewed: 'audit' }
export const COLUMN_ROLES: ColumnRole[] = ['build', 'review', 'audit']
export const COLUMN_TITLES: Record<ColumnRole, string> = { build: 'Build', review: 'Review', audit: 'Security audit' }

const PROVIDER_LABELS: Record<string, string> = { claude: 'Claude Code', codex: 'Codex' }
const ATTENTION_STATES = ['approval_needed', 'hold', 'failed']

export function maxAge(runtime: { cycle_seconds?: number } | null | undefined): number {
  return Math.max(120, (Number(runtime?.cycle_seconds) || 30) * 3)
}

export function isFresh(view: FactoryView | null | undefined, now: number): boolean {
  const at = (view?.observation as { at?: number } | undefined)?.at
  if (!at) return false
  const age = now - at
  return age >= -5 && age < maxAge(view?.runtime)
}

export function factoryFreshness(view: FactoryView | null, now: number, disconnected = false): string {
  if (disconnected) return 'Reconnecting · last known state'
  if (!view) return 'Connecting…'
  if (!view.active) return 'Factory not running · last known state'
  const observed = view.observation?.at
  return isFresh(view, now) && observed ? `Live · checked ${elapsed(now - observed)} ago` : 'Updates delayed · last known state'
}

export function jobFor(view: FactoryView | null | undefined, issue: LedgerIssueView): JobView | null {
  if (!issue.active) return null
  return (view?.jobs ?? []).find(job => job.id === issue.active && !job.cleanup && !job.outcome) ?? null
}

export function isRunning(view: FactoryView | null | undefined, job: FactorySummary['workers'][number] | null, now: number): boolean {
  if (!job) return false
  const seen = job.observed_at ?? job.last_observed ?? null
  return job.phase === 'running' && job.observed_status === 'running' && !job.cleanup && !job.fenced && !job.outcome
    && isFresh(view, now) && !!seen && now - seen >= -5 && now - seen < maxAge(view?.runtime)
}

// Waiting for another issue is not queued work: no worker can take it until
// the dependency closes, so it is never counted or shown as waiting for one.
function waitingOnDependencies(issue: LedgerIssueView | null | undefined): boolean {
  return !!issue?.blocked_dependencies?.length
}

// The bay an issue stays in while its stage settles a finished job (#241).
function settlingRole(issue: Pick<LedgerIssueView, 'transition'> | null | undefined): ColumnRole | null {
  const role = issue?.transition?.role
  return role && (COLUMN_ROLES as string[]).includes(role) ? role as ColumnRole : null
}

export function phaseLabel(issue: LedgerIssueView | null, job: JobView | null, view: FactoryView | null | undefined, now: number): string {
  if (issue) return operatorLabel(issue)
  if (job) {
    if (job.capacity_wait_since) return 'Waiting for memory'
    if (isRunning(view, job, now)) return 'Working'
    if (job.phase === 'running') return 'Last seen working'
    if (job.phase === 'reserved' || job.phase === 'created' || job.phase === 'launching') return 'Starting'
    if (job.phase === 'external') return 'Merging'
    return 'Finishing'
  }
  return 'Waiting'
}

const JOB_ROLE_LABELS: Record<JobRole, string> = { build: 'Build', review: 'Review', audit: 'Security audit', recovery: 'Investigation', merge: 'Merge' }
const OUTCOME_LABELS: Record<string, string> = {
  accepted: 'Finished', pass: 'Passed', reject: 'Changes requested', failed: 'Failed', cancelled: 'Stopped',
  superseded: 'Replaced by a newer attempt', held: 'Put on hold', needs_decision: 'Needs your decision',
  blocked_environment: 'Environment blocked', waiting: 'Waiting', wait: 'Waiting', services_retry: 'Retrying services',
  needs_human_verification: 'Waiting for verification',
  waiting_on_issues: 'Waiting for prerequisite issues',
  memory_retry: 'Retrying with more memory', investigated: 'Investigated',
}

// One line per job in an issue's history: what the job was and where it got
// to, in operator words. Never the raw phase, and never the phase twice.
export function jobSummary(job: Pick<JobView, 'role' | 'phase' | 'outcome' | 'cleanup' | 'capacity_wait_since'>): string {
  const role = JOB_ROLE_LABELS[job.role] ?? job.role
  let state: string
  if (job.outcome) state = OUTCOME_LABELS[job.outcome] ?? job.outcome.replace(/_/g, ' ').replace(/^./, c => c.toUpperCase())
  else if (job.capacity_wait_since) state = 'Waiting for memory'
  else if (job.phase === 'external') state = 'Merging on GitHub'
  else if (job.cleanup) state = 'Cleaning up'
  else state = { reserved: 'Starting', created: 'Starting', launching: 'Starting', running: 'Running', stopping: 'Stopping', cleaned: 'Finished' }[job.phase] ?? 'Starting'
  return `${role} · ${state}`
}

function latestProvider(issue: LedgerIssueView | null): string | null {
  let provider: string | null = null
  for (const event of issue?.history ?? []) if (event.kind === 'availability_wait' && event.provider) provider = event.provider
  return provider
}

export function reasonText(issue: LedgerIssueView | null, job: JobView | null): string {
  const canonical = operatorReason(issue)
  if (canonical !== undefined) return canonical
  if (job?.capacity_wait_since) return 'Waiting for memory. Forge will start this worker automatically when space is available.'
  if (!job && issue?.ownership_wait) return issue.ownership_wait.plain
  if (!job && waitingOnDependencies(issue)) return issue!.dependency_reason ?? ''
  // An approval the operator already asked for: the card says who has it,
  // which is more use than repeating that a review is needed.
  if (issue?.state === 'approval_needed' && issue.reviewers_requested?.logins?.length) {
    return `Review requested from ${reviewerMentions(issue.reviewers_requested.logins)}`
  }
  const reason = issue?.reason ?? ''
  if (job && (reason === 'blocked_auth' || reason === 'blocked_rate_limit')) return ''
  if (reason === 'blocked_auth') {
    const provider = latestProvider(issue)
    return `${(provider && PROVIDER_LABELS[provider]) || 'A coding account'} needs sign-in. Queued for another available coding account.`
  }
  if (reason === 'blocked_rate_limit') return 'Coding account usage limit reached. Forge will retry automatically.'
  // What Forge last decided, in the words the issue page leads with. While a
  // worker is running that card belongs to the previous step, so it waits.
  const headline = (issue?.last_summary?.headline ?? '').trim()
  if (!job && headline) return clampTitle(headline)
  return reason
}

export function elapsed(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds))
  if (total < 60) return `${total} sec`
  if (total < 3600) return `${Math.floor(total / 60)} min`
  return `${Math.floor(total / 3600)}h ${Math.floor((total % 3600) / 60)}m`
}

function providerLine(job: JobView | null): string {
  if (!job?.provider) return ''
  const label = PROVIDER_LABELS[job.provider] ?? job.provider
  return job.model ? `${label} · ${job.model}` : label
}

function timerEndFor(view: FactoryView | null | undefined, job: JobView | null, running: boolean, now: number): number | null {
  if (!job) return null
  if (running) return now
  return job.observed_at ?? job.last_observed ?? (view?.observation as { at?: number } | undefined)?.at ?? null
}

function footerLinkFor(issue: LedgerIssueView, job: JobView | null): CardModel['footerLink'] {
  if (issue.prerequisite_hold) return { to: `/settings/repositories/${issue.repo_id}?setup=1`, label: 'Prepare repository →' }
  if (issue.reason === 'blocked_auth' && !job) return { to: '/settings/harnesses', label: 'Coding accounts →' }
  if (issue.failure_count > 0) return { to: '', label: `${issue.failure_count} / 3 attempts failed` }
  return null
}

function stoppable(job: JobView | null): boolean {
  return !!job && job.role !== 'merge' && !job.cleanup && !job.outcome
}

// ---------------------------------------------------------------------------
// Deployment tracking (spec "Operator UI"). A repository is tracked once its
// deployment profile is confirmed; only then does a merge that has not reached
// production mean anything, so an untracked repository says nothing at all.
// ---------------------------------------------------------------------------

function trackedRepositories(view: Pick<FactoryView, 'repositories'> | null | undefined): Set<number> {
  const tracked = new Set<number>()
  for (const repository of view?.repositories ?? []) if (repository.deployment?.profile) tracked.add(repository.id)
  return tracked
}

// The word a completed card carries in its footer.
function deploymentNote(issue: LedgerIssueView, tracked: Set<number>): string {
  if (issue.deployed) return 'deployed'
  if (issue.deploy_failed) return 'deploy failed'
  return issue.state === 'merged' && tracked.has(issue.repo_id) ? 'waiting for deploy' : ''
}

// The issue page's Deployment line: the same three states in full, with the
// deploy run when GitHub gave one. `now` is 0 until the page is mounted, and
// the sentence reads without an age until then.
export function deploymentLine(
  issue: LedgerIssueView | null | undefined,
  view: Pick<FactoryView, 'repositories'> | null | undefined,
  now: number,
): { text: string; runUrl: string | null } | null {
  if (!issue) return null
  // Sentences for the issue panel: when, and on what evidence; no commit hash.
  const when = (at: number) => (now ? ' ' + spelledAgo(now - at) : '')
  if (issue.deployed) {
    const via = issue.deployed.via === 'github' ? 'a GitHub deployment' : 'the live site'
    return { text: `This change was deployed${when(issue.deployed.at)}, according to ${via}.`, runUrl: null }
  }
  if (issue.deploy_failed) {
    return { text: `Deploying this change failed${when(issue.deploy_failed.at)}.`, runUrl: issue.deploy_failed.run_url ?? null }
  }
  if (issue.state === 'merged' && trackedRepositories(view).has(issue.repo_id)) return { text: 'Waiting for this change to be deployed.', runUrl: null }
  return null
}

function issueCard(issue: LedgerIssueView, job: JobView | null, view: FactoryView, now: number, tracked: Set<number>): CardModel {
  const running = isRunning(view, job, now)
  return {
    key: issue.key,
    id: `issue-${issue.repo_id}-${issue.number}`,
    to: `/issues/${encodeURIComponent(issue.key)}`,
    repo: issue.repo,
    number: issue.number,
    title: clampTitle(issue.title || `Issue #${issue.number}`),
    operator_status: issue.operator_status,
    phaseLabel: phaseLabel(issue, job, view, now),
    providerLine: providerLine(job),
    reason: reasonText(issue, job),
    timerStart: job?.started_at ?? null,
    timerEnd: timerEndFor(view, job, running, now),
    running,
    stoppable: stoppable(job),
    jobId: job?.id ?? null,
    footerLink: footerLinkFor(issue, job),
    deployment: deploymentNote(issue, tracked),
    state: issue.state,
    look: ledgerLook(issue, job, running),
    summary: (issue.last_summary?.headline ?? '').trim(),
    pr: issue.candidate?.pr ?? null,
    failures: issue.failure_count ?? 0,
    updatedAt: lastEventAt(issue),
    doneAt: doneAt(issue),
  }
}

function lastEventAt(issue: LedgerIssueView): number | null {
  const at = (issue.history ?? []).reduce((latest, event) => Math.max(latest, Number(event.at) || 0), 0)
  return at || null
}

// When a completed issue left the line: the server's `completed_at` on a
// paged read, else its last merge or cancellation in the history tail.
export function doneAt(issue: LedgerIssueView): number | null {
  if (issue.state !== 'merged' && issue.state !== 'cancelled') return null
  if (issue.completed_at) return issue.completed_at
  const events = (issue.history ?? []).filter(event => event.kind === 'merged' || event.kind === 'cancelled')
  return Number(events[events.length - 1]?.at) || null
}

// Adapter for an internal job (recovery, cleanup or an orphaned attempt) that
// has no LedgerIssueView of its own: it keeps a route to its logs through
// /issues/<issue_key>?job=<id>, which Task 7 renders.
export function toCard(job: JobView, view: FactoryView, now: number): CardModel {
  const running = isRunning(view, job, now)
  const navigationIssue = job.issue_key ?? job.members?.[0]?.issue_key
  return {
    key: job.issue_key ?? `delivery-${job.unit_id ?? job.id}`,
    id: `job-${job.id}`,
    to: navigationIssue ? `/issues/${encodeURIComponent(navigationIssue)}?job=${job.id}` : '/issues',
    repo: job.repo,
    number: null,
    title: job.issue_key ? clampTitle(job.issue_key) : `Delivery · ${job.members?.length ?? 0} issues`,
    phaseLabel: phaseLabel(null, job, view, now),
    providerLine: providerLine(job),
    reason: '',
    timerStart: job.started_at ?? null,
    timerEnd: timerEndFor(view, job, running, now),
    running,
    stoppable: stoppable(job),
    jobId: job.id,
    footerLink: null,
    deployment: '',
    state: null,
    look: { stage: 'queued', mood: running ? 'busy' : 'open', accessory: null, beacon: false, zzz: false, scorches: 0, confetti: false },
    summary: '',
    pr: null,
    failures: 0,
    updatedAt: job.observed_at ?? job.last_observed ?? job.created_at ?? null,
    doneAt: null,
  }
}

function eligible(view: FactoryView, issue: LedgerIssueView): boolean {
  const repositories = view.repositories
  if (!Array.isArray(repositories)) return true
  const row = repositories.find(repo => repo.id === issue.repo_id || repo.name === issue.repo)
  return !row || row.enabled
}

export interface IssueGroups {
  columns: Record<ColumnRole, CardModel[]>
  // Waiting on the operator (approval needed, failed): every one of these is
  // read. A deliberate hold is not, so it is kept apart and stacked.
  attention: CardModel[]
  held: CardModel[]
  owned: CardModel[]
  // Blocked by open dependencies: it resumes by itself when they close.
  waiting: CardModel[]
  finishing: CardModel[]
  investigating: CardModel[]
  internalJobs: CardModel[]
  completed: CardModel[]
}

// Assignment order: columns -> held -> attention -> finishing -> runners -> completed.
// An issue appears exactly once. Internal jobs are listed separately, by job.
export function groupIssues(view: FactoryView | null | undefined, now: number): IssueGroups {
  const groups: IssueGroups = {
    columns: { build: [], review: [], audit: [] },
    attention: [], held: [], owned: [], waiting: [], finishing: [], investigating: [], internalJobs: [], completed: [],
  }
  if (!view) return groups
  const tracked = trackedRepositories(view)
  for (const issue of view.issues ?? []) {
    const job = jobFor(view, issue)
    const card = issueCard(issue, job, view, now, tracked)
    // An issue with a live job belongs to the column of that job's role; one
    // settling a finished job stays at that job's machine; otherwise it waits
    // in the column of the stage that comes next.
    const settling = job ? null : settlingRole(issue)
    if (settling) { groups.columns[settling].push({ ...card, checking: true }); continue }
    if (!job && issue.ownership_wait && !['merged', 'cancelled', 'failed', 'hold'].includes(issue.state)) { groups.owned.push(card); continue }
    if (!job && waitingOnDependencies(issue) && NEXT_ROLE[issue.state]) { groups.waiting.push(card); continue }
    const role = job && COLUMN_ROLES.includes(job.role as ColumnRole)
      ? job.role as ColumnRole
      : NEXT_ROLE[issue.state] ?? null
    if (role) { groups.columns[role].push(card); continue }
    // A held issue counts as needing attention (the breakdown row and the
    // section header both count it) but waits as a stack, not as a card.
    if (issue.state === 'hold') { groups.held.push(card); continue }
    if (ATTENTION_STATES.includes(issue.state)) { groups.attention.push(card); continue }
    if (issue.state === 'audited' || job?.role === 'merge') { groups.finishing.push(card); continue }
    if (issue.state === 'investigating') { groups.investigating.push(card); continue }
    if (issue.state === 'merged' || issue.state === 'cancelled') groups.completed.push(card)
  }
  // Internal rows are the jobs no issue points at — a recovery investigation
  // an issue already owns is rendered once, as that issue's card.
  const active = new Set((view.issues ?? []).map(issue => issue.active).filter(Boolean))
  for (const job of view.jobs ?? []) {
    if (!active.has(job.id)) groups.internalJobs.push(toCard(job, view, now))
  }
  return groups
}

export interface WorkerUsage {
  occupied: number; running: number
  roles: Record<ColumnRole, { occupied: number; running: number }>
}

// A delivery may put several issue cards at one machine. Capacity belongs to
// distinct leased jobs, including starting/finishing work, never to those cards.
export function workerUsage(view: FactoryView, now: number): WorkerUsage {
  const roles = { build: { occupied: 0, running: 0 }, review: { occupied: 0, running: 0 }, audit: { occupied: 0, running: 0 } }
  const seen = new Set<string>()
  for (const job of view.summary?.workers ?? view.jobs ?? []) {
    if (job.cleanup || !COLUMN_ROLES.includes(job.role as ColumnRole) || seen.has(job.id)) continue
    seen.add(job.id)
    const role = roles[job.role as ColumnRole]
    role.occupied++
    if (isRunning(view, job, now)) role.running++
  }
  return { roles, occupied: Object.values(roles).reduce((n, value) => n + value.occupied, 0),
    running: Object.values(roles).reduce((n, value) => n + value.running, 0) }
}

// A merge count only means something over a time frame. Whether the merge
// reached production is not tracked here, so a count never claims it.
function mergedSince(view: FactoryView, now: number, seconds: number): number {
  const mergedAt = (issue: LedgerIssueView): number => {
    const events = (issue.history ?? []).filter(event => event.kind === 'merged')
    return Number(events[events.length - 1]?.at ?? 0)
  }
  return (view.issues ?? []).filter(issue => issue.state === 'merged' && mergedAt(issue) >= now - seconds).length
}

export function mergedToday(view: FactoryView | null | undefined, now: number): number {
  if (!view) return 0
  return view.summary?.merged_day ?? mergedSince(view, now, 24 * 3600)
}

// The one-line readout under the factory line: how many workers are busy out
// of the slots there are, what is queued, what waits to merge and what merged
// today. "Needs you" is not counted here: it is the strip's crate, so the two
// always agree (factoryLine.ts lineAnalyse().attention).
interface Readout { occupied: number; working: number; slots: number; queued: number; merging: number; merged: number }

export function readout(view: FactoryView | null | undefined, now: number): Readout | null {
  if (!view) return null
  const slots = view.runtime?.slots ?? {}
  const roles: JobRole[] = ['build', 'review', 'audit']
  const issues = view.issues ?? []
  const usage = workerUsage(view, now)
  return {
    occupied: usage.occupied, working: usage.running,
    slots: roles.reduce((total, role) => total + (Number(slots[role]) || 0), 0),
    queued: issues.filter(issue => !jobFor(view, issue) && !settlingRole(issue) && NEXT_ROLE[issue.state] && eligible(view, issue) && !waitingOnDependencies(issue) && !issue.ownership_wait).length,
    merging: issues.filter(issue => issue.state === 'audited' && !issue.ownership_wait).length,
    merged: mergedToday(view, now),
  }
}

type WarningKind = 'github' | 'capacity' | 'runner' | 'reconciliation' | 'other'
export interface Warning { kind: WarningKind; text: string; to: string; label: string }

const PLANNING_WARNING_TEXT = new Map([
  ['triage_result_missing', 'The planning worker finished without a result.'],
  ['triage_result_invalid', 'The planning worker returned an invalid result.'],
  ['triage_worker_unsuccessful', 'The planning worker could not complete its proposal.'],
  ['triage_proposal_invalid', 'The planning proposal could not be validated.'],
  ['triage_capture_invalid', 'The planning worker’s evidence could not be saved.'],
  ['triage_worker_unavailable', 'The planning worker or its GitHub access is unavailable.'],
  ['triage_inputs_changed', 'Repository information changed before planning could start.'],
])

// [primary, fallback-when-primary-is-the-current-route]
const WARNING_DESTINATIONS: Record<WarningKind, [[string, string], [string, string]]> = {
  github: [['/settings/github', 'Open GitHub access'], ['/setup-help#github-repository-protection', 'Open help']],
  capacity: [['/settings', 'Open worker capacity'], ['/settings#worker-capacity', 'Open worker capacity']],
  runner: [['/setup-help#host-installation', 'Check service setup'], ['/setup-help', 'Open help']],
  reconciliation: [['/setup-help#configuration-and-recovery', 'Open help'], ['/setup-help', 'Open help']],
  other: [['/setup-help', 'Open help'], ['', '']],
}

export function destination(kind: WarningKind, currentPath: string): { to: string; label: string } {
  const [primary, fallback] = WARNING_DESTINATIONS[kind]
  const [to, label] = primary[0] === currentPath ? fallback : primary
  return { to, label }
}

export function warnings(
  // warnings() reads error/checks, statusRows() (utils/status.ts) reads
  // phase/harnesses/qualification_fresh/service: both take the one
  // OnboardingView that utils/onboarding.ts owns, so no second name for the
  // same payload shadows it in Nuxt's auto-import registry.
  view: { factory: FactoryView | null; onboarding?: OnboardingView | null },
  now: number,
  currentPath: string,
): Warning[] {
  const out: Warning[] = []
  const seen = new Set<string>()
  for (const error of (view.factory?.observation as { errors?: ObservationError[] } | undefined)?.errors ?? []) {
    if (seen.has(error.reason)) continue
    seen.add(error.reason)
    const kind: WarningKind = (['github', 'capacity', 'runner', 'reconciliation'] as string[])
      .includes(error.phase) ? (error.phase as WarningKind) : 'other'
    const text = PLANNING_WARNING_TEXT.get(error.reason) ?? error.reason
    out.push({ kind, text, ...destination(kind, currentPath) })
  }
  for (const action of view.factory?.actions ?? []) {
    if (action.status !== 'rejected' || !action.error) continue
    const text = 'Action could not be applied: ' + action.error
    if (seen.has(text)) continue
    seen.add(text)
    out.push({ kind: 'other', text, ...destination('other', currentPath) })
  }
  if (view.onboarding?.error && (view.onboarding.checks?.provider === 'failed' || view.onboarding.checks?.runner === 'failed'))
    out.push({ kind: 'runner', text: view.onboarding.error, to: '/settings/harnesses', label: 'Open coding accounts' })
  return out
}

// The operator status line repeats the board when all it says is that a
// worker is busy on an issue; the board already shows that card. Only
// waits, attention and empty states are worth a line above the boxes.
const PROGRESS_STATUS = /^(Building|Reviewing|Auditing|Merging|Investigating|Working on) issue #\d+$/
export function isProgressStatus(title: string | null | undefined): boolean {
  return PROGRESS_STATUS.test(title ?? '')
}
