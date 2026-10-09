// What a running worker is doing, for the issue panel's Details tab: the
// worker output payload (GET /api/factory/output, forge/factory/store_views.py
// output()), how alive the worker is, the bubble the cube says, the facts
// under it and the actions in their order of importance. Pure, so the copy
// the operator reads is decided (and tested) here, not in templates.
import { elapsed, type JobRole, type JobView } from './factory'
import { modelLabel, persona } from './personas'
import { summaryBlock, statusLabel, type IssueAction, type IssueDetail } from './issues'
import type { StageId } from './pipeline'

// ---------------------------------------------------------------------------
// Worker output
// ---------------------------------------------------------------------------

export interface OutputActivity { text: string; tool: string | null; at: number | null }
export type OutputUnavailable = 'not_started' | 'container_missing' | 'runner_unavailable'
export interface OutputView {
  ok: true
  // Raw text: captured logs once collected, else the container's live tail.
  logs: string
  // The same text without the entrypoint's heartbeat lines (newer servers).
  text?: string
  // True while the job runs and Forge reads its container rather than a capture.
  live?: boolean
  // Server time of the live read, epoch seconds.
  at?: number
  running?: boolean
  unavailable?: OutputUnavailable
  activity?: OutputActivity | null
  heartbeat_at?: number | null
  expired_at?: number
}

const HEARTBEAT = /^\s*\{\s*"runner"\s*:\s*"alive"/
export function isHeartbeat(line: string): boolean { return HEARTBEAT.test(line) }

// Heartbeats are proof of life, not output: the readable view hides them.
export function outputText(view: Pick<OutputView, 'logs' | 'text'> | null | undefined, raw = false): string {
  if (!view) return ''
  if (raw) return view.logs ?? ''
  if (typeof view.text === 'string') return view.text
  return (view.logs ?? '').split('\n').filter(line => !isHeartbeat(line)).join('\n')
}

export const UNAVAILABLE_TEXT: Record<OutputUnavailable, string> = {
  not_started: 'The worker is starting. Its output appears here once its container runs.',
  container_missing: 'The worker’s container is gone. Its output appears here once Forge records it.',
  runner_unavailable: 'Forge could not read the worker’s output just now. It tries again on the next refresh.',
}

export function emptyOutputText(view: OutputView | null | undefined): string {
  if (view?.unavailable) return UNAVAILABLE_TEXT[view.unavailable] ?? UNAVAILABLE_TEXT.runner_unavailable
  if (view?.expired_at) return 'This output has expired and is no longer kept.'
  if (view?.live) return 'The worker has not printed anything yet.'
  return 'No captured output is available yet.'
}

// A time the server read, as an age the browser can count on from: the
// server's own clock for the gap, the browser's for the time since the read.
// Neither clock has to agree with the other.
export function serverAge(eventAt: number | null | undefined, view: Pick<OutputView, 'at'> | null | undefined,
  fetchedMs: number, nowMs: number): number | null {
  if (!eventAt || !view?.at) return null
  return Math.max(0, view.at - eventAt) + Math.max(0, (nowMs - fetchedMs) / 1000)
}

// ---------------------------------------------------------------------------
// Liveness
// ---------------------------------------------------------------------------

// The entrypoint prints a heartbeat every five seconds; a minute of silence
// is twelve missed beats.
export const STALLED_AFTER = 60
export interface Liveness { state: 'alive' | 'stalled' | 'unknown'; text: string }

export function liveness(heartbeatAge: number | null): Liveness {
  if (heartbeatAge === null) return { state: 'unknown', text: '' }
  if (heartbeatAge > STALLED_AFTER) return { state: 'stalled', text: `no heartbeat for ${elapsed(heartbeatAge)}` }
  return { state: 'alive', text: `alive ${Math.round(heartbeatAge)}s ago` }
}

// ---------------------------------------------------------------------------
// The bubble
// ---------------------------------------------------------------------------

// "Running pnpm build" → Running `pnpm build`: the part the worker typed is
// code, the verb is prose. Lines already quoted keep their quotes.
export interface Segment { text: string; code: boolean }
const CODE_AFTER = /^(Running|Editing|Reading|Opening) (.+)$/
export function activitySegments(line: string): Segment[] {
  if (line.includes('`')) {
    return line.split('`').map((text, index) => ({ text, code: index % 2 === 1 })).filter(part => part.text)
  }
  const match = CODE_AFTER.exec(line)
  if (match && !/^(the|a|an) /.test(match[2]!)) return [{ text: match[1]! + ' ', code: false }, { text: match[2]!, code: true }]
  return [{ text: line, code: false }]
}

// What each kind of worker is doing when there is no live line to say.
const ROLE_DOING: Record<JobRole, string> = {
  build: 'is building the change',
  review: 'is reviewing the pull request',
  audit: 'is running the security review',
  recovery: 'is looking into the failure',
  merge: 'is merging the pull request',
}
const STAGE_ROLE: Partial<Record<StageId, JobRole>> = { build: 'build', review: 'review', audit: 'audit', approval: 'merge' }

export interface Bubble {
  // The line the cube says. `segments` carries the live activity, quoted.
  headline: string
  segments: Segment[] | null
  // "for 2 min": how long the current activity has run.
  since: string
  plain: string
  // The worker went quiet: the bubble says so instead of a stale line.
  stalled: boolean
}

export interface BubbleInput {
  detail: Pick<IssueDetail, 'status' | 'summary' | 'explanation' | 'environment_blocker' | 'operator_status' | 'dependencies'>
  job: Pick<JobView, 'role' | 'provider'> | null
  stage: StageId
  output: OutputView | null
  activityAge: number | null
  heartbeatAge: number | null
}

function same(left: string, right: string): boolean {
  return left.trim().replace(/[.!]$/, '').toLowerCase() === right.trim().replace(/[.!]$/, '').toLowerCase()
}

// Never repeat what the badge already says: "Working" / "Building" / "Building".
function informative(line: string, shown: string[]): boolean {
  return !!line.trim() && !shown.some(other => other && same(line, other))
}

export function bubbleFor(input: BubbleInput): Bubble {
  const { detail, job, output } = input
  const label = statusLabel(detail)
  const reason = detail.operator_status?.reason ?? ''
  const block = summaryBlock(detail)
  if (detail.status === 'working') {
    const who = persona(job?.provider).name
    const role = job?.role ?? STAGE_ROLE[input.stage] ?? 'build'
    const fallback = job?.provider ? `${who} ${ROLE_DOING[role]}` : `Forge ${ROLE_DOING[role]}`
    const live = liveness(input.heartbeatAge)
    const extra = [block?.headline ?? '', block?.plain ?? ''].find(line => informative(line, [label, reason, fallback])) ?? ''
    if (live.state === 'stalled') {
      return { headline: `${who} has gone quiet`, segments: null, since: '', stalled: true,
        plain: `No heartbeat for ${elapsed(input.heartbeatAge ?? 0)}. Forge stops the attempt at its deadline if it does not recover.` }
    }
    const activity = output?.activity
    if (activity?.text) {
      return { headline: activity.text, segments: activitySegments(activity.text), stalled: false,
        since: input.activityAge !== null ? `for ${elapsed(input.activityAge)}` : '', plain: extra }
    }
    // No live line: Forge's own summary when it says more than the status,
    // else what this kind of worker does.
    const waiting = output?.unavailable ? UNAVAILABLE_TEXT[output.unavailable] : ''
    const headline = extra || fallback
    const plain = [block?.plain ?? ''].find(line => informative(line, [label, reason, headline])) ?? ''
    return { headline, segments: null, since: '', stalled: false, plain: plain || waiting }
  }
  const headline = [block?.headline ?? '', reason].find(line => informative(line, [label])) ?? (block?.headline || reason || label)
  const plain = [block?.plain ?? '', reason].find(line => informative(line, [label, headline])) ?? ''
  return { headline, segments: null, since: '', stalled: false, plain }
}

// ---------------------------------------------------------------------------
// Facts while a worker runs
// ---------------------------------------------------------------------------

export interface WorkFacts {
  who: string
  model: string
  provider: string | null
  // "Build · attempt 2 of 3"
  attempt: string
  // "12 min in · 18 min left" / "past its deadline"
  timing: string
  overdue: boolean
  branch: string | null
}

const ROLE_NAMES: Record<JobRole, string> = { build: 'Build', review: 'Review', audit: 'Security review', recovery: 'Investigation', merge: 'Merge' }

export function workFacts(job: Pick<JobView, 'role' | 'provider' | 'model' | 'created_at' | 'started_at' | 'deadline'> & { branch?: string | null },
  attempts: { failed: number; limit: number } | null | undefined, now: number): WorkFacts {
  const who = persona(job.provider)
  const started = job.started_at ?? job.created_at
  const parts: string[] = []
  if (started && now) parts.push(`${elapsed(now - started)} in`)
  const overdue = !!(job.deadline && now && now >= job.deadline)
  if (job.deadline && now) parts.push(overdue ? 'past its deadline' : `${elapsed(job.deadline - now)} left`)
  const limit = attempts?.limit ?? 0
  const attempt = job.role === 'build' && limit
    ? `${ROLE_NAMES.build} · attempt ${Math.min((attempts?.failed ?? 0) + 1, limit)} of ${limit}`
    : ROLE_NAMES[job.role] ?? job.role
  return { who: who.name, provider: job.provider ?? null, model: modelLabel(job.model, job.provider), attempt,
    timing: parts.join(' · '), overdue, branch: job.branch ?? null }
}

// ---------------------------------------------------------------------------
// Stage timing
// ---------------------------------------------------------------------------

export interface StageTime { started: number; took: number | null }
const ROLE_STAGE: Partial<Record<JobRole, StageId>> = { build: 'build', review: 'review', audit: 'audit', merge: 'approval' }
const ORDER: StageId[] = ['build', 'review', 'audit', 'approval', 'merged']

// When each stage's first worker started, and how long until the next stage's
// did: the jobs the factory view lists are all the evidence there is.
export function stageTimes(jobs: Pick<JobView, 'role' | 'created_at' | 'started_at'>[] | null | undefined): Partial<Record<StageId, StageTime>> {
  const starts: Partial<Record<StageId, number>> = {}
  for (const job of jobs ?? []) {
    const stage = ROLE_STAGE[job.role]
    const at = job.started_at ?? job.created_at
    if (!stage || !at) continue
    starts[stage] = Math.min(starts[stage] ?? Infinity, at)
  }
  const out: Partial<Record<StageId, StageTime>> = {}
  ORDER.forEach((stage, index) => {
    const started = starts[stage]
    if (!started) return
    const next = ORDER.slice(index + 1).map(id => starts[id]).find((at): at is number => !!at)
    out[stage] = { started, took: next && next >= started ? next - started : null }
  })
  return out
}

// ---------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------

export type PrimaryAction = 'approve' | 'setup' | 'discuss' | IssueAction
export interface ActionPlan {
  primary: PrimaryAction | null
  // Reversible decisions, in a row next to the primary.
  secondary: IssueAction[]
  // What talking to an agent about the issue is called here.
  ask: string
  // Ending work: behind "More", each confirmed in its own dialog.
  destructive: ('stop' | 'reset' | 'close')[]
}

// The way forward, in the order the issue list names it (issues.ts rowPrimary).
const PRIMARY_ORDER: IssueAction[] = ['assign_reviewer', 'retry', 'recheck', 'resume', 'queue']

export function actionPlan(detail: Pick<IssueDetail, 'status' | 'actions' | 'active' | 'closable'> & { approval?: unknown },
  options: { setupNeeded: boolean; internalJob: boolean; agent: string | null }): ActionPlan {
  const actions = detail.actions ?? []
  const attention = detail.status === 'attention'
  let primary: PrimaryAction | null = null
  if (detail.approval) primary = 'approve'
  else primary = PRIMARY_ORDER.find(action => (actions as IssueAction[]).includes(action)) ?? null
  if (!primary && options.setupNeeded) primary = 'setup'
  // Needs a person but offers no button: working it out together is the way on.
  if (!primary && attention) primary = 'discuss'
  const secondary = (actions as IssueAction[]).filter(action => action !== primary)
  const ask = attention ? 'Discuss blocker' : options.agent ? `Ask ${options.agent}` : 'Discuss'
  const destructive: ActionPlan['destructive'] = []
  if (detail.active && detail.status === 'working') destructive.push('stop')
  if (options.internalJob) destructive.push('reset')
  if (detail.closable) destructive.push('close')
  return { primary, secondary, ask, destructive }
}
