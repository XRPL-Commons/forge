// Where an issue is in Forge's pipeline, for the workbench at the top of the
// issue page (utils/workbench.ts) and its attempt line: Queued → Build → Review → Security review → Approval → Merged. The API
// sends the operator status and the lifecycle stage; the reviews and the pull
// request place an issue the stage does not name.
import type { IssueReview, IssueRow } from './issues'

export type StageId = 'queued' | 'build' | 'review' | 'audit' | 'approval' | 'merged'
// done: passed; active: a worker is on it; waiting: Forge will carry on by
// itself; failed / blocked: it stopped here and needs the operator.
type StepState = 'done' | 'active' | 'waiting' | 'failed' | 'blocked' | 'upcoming'

interface PipelineStep { id: StageId; label: string; short: string; state: StepState }
export interface PipelineView {
  steps: PipelineStep[]
  // null when the issue left the pipeline without merging (closed on GitHub).
  current: StageId | null
  state: StepState | 'closed'
  // A few words on the current stage: "Failed", "Waiting for approval", …
  note: string
  // "Attempt 3 of 3 failed." when attempts have been charged; empty otherwise.
  attempts: string
}

export const STAGES: { id: StageId; label: string; short: string }[] = [
  { id: 'queued', label: 'Queued', short: 'Queue' },
  { id: 'build', label: 'Build', short: 'Build' },
  { id: 'review', label: 'Review', short: 'Review' },
  { id: 'audit', label: 'Security review', short: 'Security' },
  { id: 'approval', label: 'Approval', short: 'Approval' },
  { id: 'merged', label: 'Merged', short: 'Merged' },
]

export type PipelineRow = Pick<IssueRow, 'status' | 'pull_requests' | 'operator_status'>
  & Partial<Pick<IssueRow, 'attempts' | 'stage_code'>>
  & { reviews?: IssueReview[] | null }

type Verdict = 'passed' | 'stopped' | null
function verdict(reviews: IssueReview[] | null | undefined, role: IssueReview['role']): Verdict {
  const review = (reviews ?? []).find(row => row.role === role)
  if (!review) return null
  return ['pass', 'passed', 'approved'].includes((review.outcome ?? '').toLowerCase()) ? 'passed' : 'stopped'
}

function hasCandidate(row: PipelineRow): boolean {
  return row.pull_requests.some(pull => pull.kind === 'candidate')
}

// The furthest stage the evidence shows the issue reached, for an issue whose
// lifecycle stage does not place it.
function reached(row: PipelineRow): StageId {
  if (verdict(row.reviews, 'audit') === 'passed') return 'approval'
  if (verdict(row.reviews, 'review') === 'passed') return 'audit'
  if (hasCandidate(row)) return 'review'
  return 'queued'
}

function locate(row: PipelineRow): { at: StageId | null; state: PipelineView['state']; note: string } {
  const { code, reason_code, reason } = row.operator_status
  if (code === 'completed') return { at: reason_code === 'merged' ? 'merged' : null, state: 'done', note: reason }
  if (code === 'dropped') return { at: null, state: 'closed', note: reason }
  if (code === 'backlog') return { at: 'queued', state: 'upcoming', note: reason }
  const stage: Record<string, StageId> = {
    building: 'build', publishing: 'build', reviewing: 'review', auditing: 'audit', merging: 'approval',
  }
  const at = stage[row.stage_code ?? ''] ?? reached(row)
  const state = code === 'working' ? 'active'
    : code === 'attention' ? (reason_code === 'budget_exhausted' ? 'failed' : 'blocked') : 'waiting'
  return { at, state, note: reason }
}

export function attemptNote(row: Pick<PipelineRow, 'status' | 'attempts'>): string {
  const attempts = row.attempts
  if (!attempts || !(attempts.failed > 0)) return ''
  if (row.status === 'working' && attempts.failed < attempts.limit) return `Attempt ${attempts.failed + 1} of ${attempts.limit} is running.`
  return `Attempt ${Math.min(attempts.failed, attempts.limit)} of ${attempts.limit} failed.`
}

export function issuePipeline(row: PipelineRow): PipelineView {
  const { at, state, note } = locate(row)
  const index = at ? STAGES.findIndex(stage => stage.id === at) : -1
  const steps = STAGES.map((stage, position): PipelineStep => ({
    ...stage,
    state: index === -1 ? 'upcoming'
      : position < index ? 'done'
        : position > index ? 'upcoming'
          : state === 'closed' ? 'upcoming' : state,
  }))
  return { steps, current: at, state, note, attempts: attemptNote(row) }
}
