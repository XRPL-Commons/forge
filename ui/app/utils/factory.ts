// Pure view logic for the Activity (Factory) page: every card-shaped surface
// renders a CardModel, every banner is produced by warnings(), so no component
// reaches into the raw FactoryView payload. Rules follow the operator API
// contracts §2 (GET /api/factory) and §3.1 (Activity page).
import { clampTitle } from './format'
import type { OnboardingView } from './onboarding'

export type JobRole = 'build' | 'review' | 'audit' | 'recovery' | 'merge'
export type ColumnRole = 'build' | 'review' | 'audit'
export type IssueState = 'ready' | 'built' | 'reviewed' | 'audited' | 'approval_needed'
  | 'investigating' | 'hold' | 'failed' | 'merged' | 'cancelled'

export interface JobView {
  id: string; issue_key: string; repo_id: number; repo: string; issue_number: number
  role: JobRole; slot: string; provider: string; provider_id: string | null; model: string | null
  phase: 'reserved' | 'created' | 'launching' | 'running' | 'stopping' | 'cleaned' | 'external'
  outcome: string | null; cleanup: boolean; fenced: boolean
  capacity_wait_since?: number | null; created_at: number; deadline: number
  started_at: number | null; observed_at: number | null; last_observed?: number
}

export interface LedgerEvent { kind: string; provider?: string; model?: string | null; [key: string]: unknown }

export interface LedgerIssueView {
  key: string; repo_id: number; repo: string; number: number; title: string
  state: IssueState; reason: string | null; active: string | null; failure_count: number
  candidate?: { pr?: number } | null
  prerequisite_hold?: { state: string } | true | null
  history?: LedgerEvent[]
}

export interface ObservationError { attempt?: string; issue?: string; phase: string; reason: string }
export interface OperatorStatus { title: string; detail: string; action_url: string | null; action_label: string | null; empty?: true }
export interface OperatorRequest { id: string; action: string; target: string; reason: string; status: 'pending' | 'applied' | 'rejected'; error?: string }

export interface FactoryView {
  ok: true; controls_token: string; active: boolean
  operator: OperatorStatus | null
  repositories?: { id: number; name: string; enabled: boolean }[]
  observation?: { at: number; errors?: ObservationError[] } | Record<string, never>
  actions?: OperatorRequest[]
  issues?: LedgerIssueView[]
  jobs?: JobView[]
  slots?: Record<string, string>
  runtime: { enabled: boolean; slots: Record<string, number>; cycle_seconds: number; [key: string]: unknown }
  // The digest CapacityForm sends back on POST /api/runtime (contracts §2).
  runtime_revision: string
}

export interface CardModel {
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
}


export const NEXT_ROLE: Record<string, ColumnRole> = { ready: 'build', built: 'review', reviewed: 'audit' }
export const COLUMN_ROLES: ColumnRole[] = ['build', 'review', 'audit']
export const COLUMN_TITLES: Record<ColumnRole, string> = { build: 'Build', review: 'Review', audit: 'Security audit' }

const STATE_LABELS: Record<string, string> = {
  ready: 'Queued', built: 'Queued', reviewed: 'Queued', audited: 'Ready to merge',
  approval_needed: 'Approval needed', investigating: 'Investigating', hold: 'On hold',
  failed: 'Needs attention', merged: 'Merged', cancelled: 'Cancelled',
}
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

export function jobFor(view: FactoryView | null | undefined, issue: LedgerIssueView): JobView | null {
  if (!issue.active) return null
  return (view?.jobs ?? []).find(job => job.id === issue.active && !job.cleanup && !job.outcome) ?? null
}

export function isRunning(view: FactoryView | null | undefined, job: JobView | null, now: number): boolean {
  if (!job) return false
  const seen = job.observed_at ?? job.last_observed ?? null
  return job.phase === 'running' && !job.cleanup && !job.fenced && !job.outcome
    && isFresh(view, now) && !!seen && now - seen < maxAge(view?.runtime)
}

export function phaseLabel(issue: LedgerIssueView | null, job: JobView | null, view: FactoryView | null | undefined, now: number): string {
  if (job) {
    if (job.capacity_wait_since) return 'Waiting for memory'
    if (isRunning(view, job, now)) return 'Working'
    if (job.phase === 'running') return 'Last seen working'
    if (job.phase === 'reserved' || job.phase === 'created' || job.phase === 'launching') return 'Starting'
    if (job.phase === 'external') return 'Merging'
    return 'Finishing'
  }
  return STATE_LABELS[issue?.state ?? ''] ?? 'Waiting'
}

function latestProvider(issue: LedgerIssueView | null): string | null {
  let provider: string | null = null
  for (const event of issue?.history ?? []) if (event.kind === 'availability_wait' && event.provider) provider = event.provider
  return provider
}

export function reasonText(issue: LedgerIssueView | null, job: JobView | null): string {
  if (job?.capacity_wait_since) return 'Waiting for memory. Forge will start this worker automatically when space is available.'
  const reason = issue?.reason ?? ''
  if (job && (reason === 'blocked_auth' || reason === 'blocked_rate_limit')) return ''
  if (reason === 'blocked_auth') {
    const provider = latestProvider(issue)
    return `${(provider && PROVIDER_LABELS[provider]) || 'A coding account'} needs sign-in. Queued for another available coding account.`
  }
  if (reason === 'blocked_rate_limit') return 'Coding account usage limit reached. Forge will retry automatically.'
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
  if (issue.prerequisite_hold) return { to: `/settings/repository?repo_id=${issue.repo_id}`, label: 'Prepare repository →' }
  if (issue.reason === 'blocked_auth' && !job) return { to: '/settings/harnesses', label: 'Coding accounts →' }
  if (issue.failure_count > 0) return { to: '', label: `${issue.failure_count} / 3 attempts failed` }
  return null
}

function stoppable(job: JobView | null): boolean {
  return !!job && job.role !== 'merge' && !job.cleanup && !job.outcome
}

function issueCard(issue: LedgerIssueView, job: JobView | null, view: FactoryView, now: number): CardModel {
  const running = isRunning(view, job, now)
  return {
    key: issue.key,
    id: `issue-${issue.repo_id}-${issue.number}`,
    to: `/issues/${encodeURIComponent(issue.key)}`,
    repo: issue.repo,
    number: issue.number,
    title: clampTitle(issue.title || `Issue #${issue.number}`),
    phaseLabel: phaseLabel(issue, job, view, now),
    providerLine: providerLine(job),
    reason: reasonText(issue, job),
    timerStart: job?.started_at ?? null,
    timerEnd: timerEndFor(view, job, running, now),
    running,
    stoppable: stoppable(job),
    jobId: job?.id ?? null,
    footerLink: footerLinkFor(issue, job),
  }
}

// Adapter for an internal job (recovery, cleanup or an orphaned attempt) that
// has no LedgerIssueView of its own: it keeps a route to its logs through
// /issues/<issue_key>?job=<id>, which Task 7 renders.
export function toCard(job: JobView, view: FactoryView, now: number): CardModel {
  const running = isRunning(view, job, now)
  return {
    key: job.issue_key,
    id: `job-${job.id}`,
    to: `/issues/${encodeURIComponent(job.issue_key)}?job=${job.id}`,
    repo: job.repo,
    number: null,
    title: clampTitle(job.issue_key),
    phaseLabel: phaseLabel(null, job, view, now),
    providerLine: providerLine(job),
    reason: '',
    timerStart: job.started_at ?? null,
    timerEnd: timerEndFor(view, job, running, now),
    running,
    stoppable: stoppable(job),
    jobId: job.id,
    footerLink: null,
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
    attention: [], held: [], finishing: [], investigating: [], internalJobs: [], completed: [],
  }
  if (!view) return groups
  for (const issue of view.issues ?? []) {
    const job = jobFor(view, issue)
    const card = issueCard(issue, job, view, now)
    // An issue with a live job belongs to the column of that job's role;
    // without one it waits in the column of the stage that comes next.
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

function runningJobs(view: FactoryView, now: number): JobView[] {
  return (view.jobs ?? []).filter(job => isRunning(view, job, now))
}

// One row per state an issue can be in, with a count and a sentence that
// says what the count means right now, derived from the reasons the ledger
// records. This replaces per-role tiles: the operator reads why work is not
// moving, not how many slots exist.
// `detail` is the fixed phrase printed under the number, so a box reads the
// same from one poll to the next; `meaning` is the derived sentence, shown on
// hover and to a screen reader through aria-describedby.
export interface BreakdownRow {
  id: 'working' | 'queued' | 'merging' | 'attention' | 'merged'
  label: string; value: number; detail: string; meaning: string; to: string; tone: 'ok' | 'warn' | 'muted'
}

const HOLD_REASONS: [RegExp, string][] = [
  [/environment needs attention \(toolchain\)/, 'toolchain'],
  [/environment needs attention \(resources\)/, 'memory'],
  [/environment needs attention \(sandbox\)/, 'sandbox'],
  [/environment needs attention/, 'environment'],
  [/change of approach/, 'retry limit'],
  [/hold or dependency label/, 'label'],
  [/Paused by an operator|Not queued/, 'paused'],
  [/approving reviews|protection|branch rules/, 'branch rules'],
]

function holdReason(issue: LedgerIssueView): string {
  if (issue.state === 'approval_needed') return 'approval'
  if (issue.state === 'failed') return 'failed'
  const reason = issue.reason ?? ''
  return HOLD_REASONS.find(([pattern]) => pattern.test(reason))?.[1] ?? 'decision'
}

function tally(items: string[]): string {
  const counts = new Map<string, number>()
  for (const item of items) counts.set(item, (counts.get(item) ?? 0) + 1)
  return [...counts.entries()].sort((a, b) => b[1] - a[1]).map(([name, n]) => `${n} ${name}`).join(' · ')
}

export function breakdown(view: FactoryView | null | undefined, now: number): BreakdownRow[] {
  if (!view) return []
  const slots = view.runtime?.slots ?? {}
  const issues = view.issues ?? []
  const running = runningJobs(view, now)
  const count = (role: JobRole) => running.filter(job => job.role === role).length
  const enabled = view.runtime?.enabled !== false
  const working = count('build') + count('review') + count('audit')
  const verbs: [JobRole, string][] = [['build', 'building'], ['review', 'reviewing'], ['audit', 'auditing']]
  const workingMeaning = verbs.map(([role, verb]) => `${count(role)} of ${slots[role] ?? 0} ${verb}`).join(' · ')
    + (count('recovery') ? ` · ${count('recovery')} investigating` : '')
  const queued = issues.filter(issue => !jobFor(view, issue) && NEXT_ROLE[issue.state] && eligible(view, issue)).length
  const merging = issues.filter(issue => issue.state === 'audited').length
  const refused = (view.observation?.errors ?? []).filter(error => /refused to merge/.test(error.reason)).length
  const attention = issues.filter(issue => ATTENTION_STATES.includes(issue.state))
  // A merge count only means something over a time frame. Whether the merge
  // reached production is not tracked here, so the row never claims it.
  const mergedAt = (issue: LedgerIssueView): number => {
    const events = (issue.history ?? []).filter(event => event.kind === 'merged')
    return Number(events[events.length - 1]?.at ?? 0)
  }
  const mergedIssues = issues.filter(issue => issue.state === 'merged')
  const mergedSince = (seconds: number) => mergedIssues.filter(issue => mergedAt(issue) >= now - seconds).length
  const mergedDay = mergedSince(24 * 3600)
  const mergedWeek = mergedSince(7 * 24 * 3600)
  const workerSlots = (['build', 'review', 'audit'] as JobRole[]).reduce((total, role) => total + (slots[role] ?? 0), 0)
  return [
    { id: 'working', label: 'Working', value: working, detail: `of ${workerSlots} slots`,
      meaning: workingMeaning, to: '#columns', tone: 'ok' },
    { id: 'queued', label: 'Queued', value: queued, detail: enabled ? 'waiting for a worker' : 'dispatch is paused',
      meaning: !enabled ? 'Dispatch is paused' : queued ? 'Waiting for a free worker' : 'Nothing waiting',
      to: '#columns', tone: enabled ? 'ok' : 'muted' },
    { id: 'merging', label: 'Waiting to merge', value: merging, detail: 'approved, not merged',
      meaning: refused ? `${refused} refused by branch rules` : merging ? 'Checking merge requirements' : 'Nothing approved yet',
      to: '#finishing', tone: refused ? 'warn' : 'ok' },
    { id: 'attention', label: 'Needs a decision', value: attention.length, detail: 'awaiting you',
      meaning: attention.length ? tally(attention.map(holdReason)) : 'Nothing needs you',
      to: '#needs-attention', tone: attention.length ? 'warn' : 'ok' },
    { id: 'merged', label: 'Merged today', value: mergedDay, detail: 'in the last 24 h',
      meaning: `${mergedWeek} in the last 7 days · ${mergedIssues.length} tracked in all`, to: '#completed', tone: 'ok' },
  ]
}

export type WarningKind = 'github' | 'capacity' | 'runner' | 'reconciliation' | 'other'
export interface Warning { kind: WarningKind; text: string; to: string; label: string }

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
    out.push({ kind, text: error.reason, ...destination(kind, currentPath) })
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
