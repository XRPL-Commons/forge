// What the workbench at the top of the issue panel shows: the cube's stage
// colour, its mood, what it wears and the props around it, the stages it has
// earned and the attempts it has left. Derived only from the issue payload,
// with the stage from the pipeline mapping (pipeline.ts).
import type { IssueRow } from './issues'
import { STAGES, issuePipeline, type PipelineRow, type StageId } from './pipeline'
import type { BenchScene, CubeAccessory, CubeMood } from './cube'

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

const ACCESSORY: Partial<Record<StageId, CubeAccessory>> = { build: 'hat', review: 'magnifier', approval: 'badge', merged: 'bow' }
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
    dependencies: (row.dependencies ?? []).filter(number => Number.isFinite(number)),
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
