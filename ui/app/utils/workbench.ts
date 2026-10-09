// What the workbench at the top of the issue panel shows: the cube's stage
// colour, its mood, what it wears and the props around it, the stages it has
// earned and the attempts it has left. Derived only from the issue payload,
// with the stage from the pipeline mapping (pipeline.ts).
import { blockingIssues, type IssueRow } from './issues'
import { STAGES, issuePipeline, type PipelineRow, type StageId } from './pipeline'
import type { BenchScene, CubeAccessory, CubeMood, PortraitLook } from './cube'
import type { IssueState, JobRole } from './factory'

export type WorkbenchRow = PipelineRow & Pick<IssueRow, 'number' | 'dependencies'> & Partial<Pick<IssueRow, 'held_by'>>

export type BadgeState = 'done' | 'current' | 'future'
export interface StageBadge { id: StageId; label: string; state: BadgeState }
export interface AttemptLives { lost: number; left: number; limit: number; text: string }

export interface WorkbenchLook {
  stage: StageId
  mood: CubeMood
  // The issue waits for the operator: amber light, amber bubble border.
  needsYou: boolean
  scene: BenchScene
  // "Cube for #50, Security stage, needs attention"
  label: string
  badges: StageBadge[]
  // null when the payload does not carry attempts (older servers).
  attempts: AttemptLives | null
}

export const ACCESSORY: Partial<Record<StageId, CubeAccessory>> = { build: 'hat', review: 'magnifier', approval: 'badge', merged: 'bow' }
// The stage names the operator reads on the badges and in the label.
export const STAGE_SHORT: Record<StageId, string> = Object.fromEntries(STAGES.map(stage => [stage.id, stage.short])) as Record<StageId, string>
// The badges a cube earns: every stage after the queue.
const EARNED: StageId[] = ['build', 'review', 'audit', 'approval', 'merged']

function onHold(row: WorkbenchRow): boolean {
  return (row.actions ?? []).includes('resume') || (row.held_by ?? []).length > 0
}

export function workbenchLook(row: WorkbenchRow): WorkbenchLook {
  const pipeline = issuePipeline(row)
  // An issue closed on GitHub without merging left the line: grey, at rest.
  const stage: StageId = pipeline.current ?? 'queued'
  const merged = pipeline.current === 'merged' && pipeline.state === 'done'
  const working = row.status === 'working'
  // Waiting on other issues never asks anything of the operator.
  const blocked = row.status === 'blocked'
  const dropped = row.status === 'dropped'
  const needsYou = row.status === 'attention'
  const failed = pipeline.state === 'failed'
  const awaitingApproval = stage === 'approval' && !row.merge_blocker && (pipeline.state === 'blocked' || pipeline.state === 'waiting')
  const hold = onHold(row)

  let mood: CubeMood = 'open'
  let phrase = pipeline.note.toLowerCase()
  if (merged) { mood = 'happy'; phrase = 'merged' }
  else if (working) { mood = 'busy'; phrase = 'working' }
  else if (awaitingApproval) { mood = 'hopeful'; phrase = needsYou ? 'waiting for your approval' : 'waiting for approval' }
  else if (failed) { mood = 'worried'; phrase = needsYou ? 'needs attention' : 'failed' }
  else if (dropped) { mood = 'rest'; phrase = 'no longer needed' }
  else if (blocked) { mood = 'patient'; phrase = 'waiting on other issues' }
  else if (hold) { mood = 'sleep'; phrase = 'on hold' }
  else if (needsYou) { mood = 'worried'; phrase = 'needs attention' }
  else if (pipeline.state === 'closed') phrase = 'closed'

  const failures = row.attempts ? Math.max(0, Math.min(row.attempts.failed, row.attempts.limit)) : 0
  const scene: BenchScene = {
    stage,
    mood,
    accessory: ACCESSORY[stage] ?? null,
    scorches: failures,
    beacon: needsYou,
    zzz: mood === 'sleep',
    smoke: working && stage === 'build',
    toolbox: stage === 'build',
    // A blocked issue is tethered to the prerequisites still open; any other
    // issue shows every dependency its body names.
    dependencies: blocked ? blockingIssues(row) : (row.dependencies ?? []).filter(number => Number.isFinite(number)),
    confetti: merged,
  }

  const steps = new Map(pipeline.steps.map(step => [step.id, step]))
  const badges = EARNED.map((id): StageBadge => {
    const step = steps.get(id)!
    const state: BadgeState = step.state === 'done' ? 'done' : id === pipeline.current && pipeline.state !== 'closed' ? 'current' : 'future'
    return { id, label: STAGE_SHORT[id], state }
  })

  let attempts: AttemptLives | null = null
  if (row.attempts && row.attempts.limit > 0) {
    const limit = row.attempts.limit, left = limit - failures
    const text = !failures && working ? `attempt 1 of ${limit}` : `${left} of ${limit} left`
    attempts = { lost: failures, left, limit, text }
  }

  return {
    stage, mood, needsYou, scene, badges, attempts,
    label: `Cube for #${row.number}, ${STAGE_SHORT[stage]} stage, ${phrase}`,
  }
}

// The lighter look an Activity card draws from the factory ledger, which has
// no reviews or pull request list: the stage comes from the ledger state and
// the job working on it, the props from the same rules as the workbench.
const JOB_STAGE: Partial<Record<JobRole, StageId>> = { build: 'build', review: 'review', audit: 'audit', merge: 'approval' }
const STATE_STAGE: Partial<Record<IssueState, StageId>> = {
  ready: 'queued', built: 'review', reviewed: 'audit', audited: 'approval', approval_needed: 'approval', merged: 'merged', cancelled: 'queued',
}

export interface LedgerLookIssue {
  state: IssueState; failure_count?: number; candidate?: { pr?: number } | null; blocked_dependencies?: number[]
  closed_as?: ClosedAs | null
}

// GitHub's reason an issue closed. Not planned and duplicate mean the work
// is no longer needed: it left the line without being finished.
export type ClosedAs = 'not_planned' | 'duplicate' | 'completed'
export function isDropped(issue: Pick<LedgerLookIssue, 'state' | 'closed_as'>): boolean {
  return issue.state !== 'merged' && (issue.closed_as === 'not_planned' || issue.closed_as === 'duplicate')
}

export function ledgerLook(issue: LedgerLookIssue, job: { role: JobRole } | null, running: boolean): PortraitLook {
  const state = issue.state
  const failures = Math.max(0, Number(issue.failure_count) || 0)
  const dropped = isDropped(issue)
  // No longer needed: back in grey, whatever it reached.
  const stage: StageId = state === 'merged' ? 'merged' : dropped ? 'queued'
    : (job && JOB_STAGE[job.role]) || STATE_STAGE[state]
      // Held, failed or under investigation: as far as the evidence shows.
      || (issue.candidate?.pr ? 'review' : failures ? 'build' : 'queued')
  let mood: CubeMood = 'open'
  if (state === 'merged') mood = 'happy'
  else if (dropped) mood = 'rest'
  else if (running) mood = 'busy'
  else if (state === 'approval_needed') mood = 'hopeful'
  else if (state === 'failed') mood = 'worried'
  else if (state === 'hold') mood = 'sleep'
  // Queued work held back by open prerequisites waits patiently, never lit.
  else if (!job && issue.blocked_dependencies?.length && ['ready', 'built', 'reviewed'].includes(state)) mood = 'patient'
  return {
    stage, mood,
    accessory: state === 'cancelled' || dropped ? null : ACCESSORY[stage] ?? null,
    beacon: !dropped && (state === 'failed' || state === 'approval_needed'),
    zzz: mood === 'sleep',
    scorches: Math.min(3, failures),
    confetti: state === 'merged',
  }
}
