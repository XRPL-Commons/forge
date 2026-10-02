// Where an issue is in Forge's pipeline, for the strip at the top of the issue
// page: Queued → Build → Review → Security review → Approval → Merged. The API
// does not send a stage, so it is derived here from what the row already
// carries: the status, Forge's own status line, the reviews, the pull request,
// the approval and the blockers. A failure or a hold is shown on the stage
// where it happened.
import type { IssueReview, IssueRow } from './issues'

export type StageId = 'queued' | 'build' | 'review' | 'audit' | 'approval' | 'merged'
// done: passed; active: a worker is on it; waiting: Forge will carry on by
// itself; failed / blocked: it stopped here and needs the operator.
export type StepState = 'done' | 'active' | 'waiting' | 'failed' | 'blocked' | 'upcoming'

export interface PipelineStep { id: StageId; label: string; short: string; state: StepState }
export interface PipelineView {
  steps: PipelineStep[]
  // null when the issue left the pipeline without merging (closed on GitHub).
  current: StageId | null
  state: StepState | 'closed'
  // A few words on the current stage: "Failed", "Waiting for approval", …
  note: string
  // "attempt 3 of 3" when attempts have been charged; empty otherwise.
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

export type PipelineRow = Pick<IssueRow, 'status' | 'explanation' | 'actions' | 'pull_requests'>
  & Partial<Pick<IssueRow, 'summary' | 'attempts' | 'approval' | 'merge_blocker' | 'environment_blocker' | 'decision_request'>>
  & { reviews?: IssueReview[] | null }

// Forge's status line for a running worker names its role (issues.py presentation()).
const WORKING: Record<string, StageId> = {
  'Building': 'build', 'Reviewing': 'review', 'Security review': 'audit', 'Merging': 'merged',
}
// …and for an issue between two stages, the stage it waits for.
const QUEUED: Record<string, StageId> = {
  'Waiting for review.': 'review', 'Waiting for security review.': 'audit', 'Checking merge requirements.': 'approval',
}

type Verdict = 'passed' | 'stopped' | null
function verdict(reviews: IssueReview[] | null | undefined, role: IssueReview['role']): Verdict {
  const review = (reviews ?? []).find(row => row.role === role)
  if (!review) return null
  return ['pass', 'passed', 'approved'].includes((review.outcome ?? '').toLowerCase()) ? 'passed' : 'stopped'
}

function hasCandidate(row: PipelineRow): boolean {
  return (row.pull_requests ?? []).some(pull => pull.kind === 'candidate' || !pull.kind)
}

// The furthest stage the evidence shows the issue reached. `started` says
// whether work began at all, so a held issue with nothing built stays Queued.
function reached(row: PipelineRow, started: boolean): StageId {
  if (verdict(row.reviews, 'audit') === 'passed') return 'approval'
  if (verdict(row.reviews, 'review') === 'passed') return 'audit'
  if (hasCandidate(row)) return 'review'
  return started ? 'build' : 'queued'
}

// Where an attempt stopped: the review that did not pass, else how far it got.
function stoppedAt(row: PipelineRow): StageId {
  const review = verdict(row.reviews, 'review'), audit = verdict(row.reviews, 'audit')
  if (review === 'stopped') return 'review'
  if (audit === 'stopped') return 'audit'
  return reached(row, true)
}

function locate(row: PipelineRow): { at: StageId | null; state: PipelineView['state']; note: string } {
  const explanation = (row.explanation ?? '').trim()
  const actions = row.actions ?? []
  const failures = row.attempts?.failed ?? 0
  switch (row.status) {
    case 'completed':
      return explanation.startsWith('Completed and merged') || /merged/i.test(row.summary?.headline ?? '')
        ? { at: 'merged', state: 'done', note: 'Merged' }
        : { at: null, state: 'closed', note: 'Closed on GitHub' }
    case 'working':
      if (explanation === 'Investigating a failed attempt') return { at: stoppedAt(row), state: 'active', note: 'Looking into the failure' }
      return { at: WORKING[explanation] ?? reached(row, true), state: 'active', note: 'In progress' }
    case 'queued':
      if (QUEUED[explanation]) return { at: QUEUED[explanation]!, state: 'waiting', note: 'Waiting' }
      if (explanation === 'Waiting for a failure investigation.') return { at: stoppedAt(row), state: 'failed', note: 'Failed, being looked into' }
      return { at: reached(row, false), state: 'waiting', note: 'Waiting' }
    case 'backlog':
      return { at: 'queued', state: 'upcoming', note: 'Not queued' }
    case 'waiting':
      return { at: reached(row, failures > 0), state: 'waiting', note: 'Waiting' }
    case 'attention':
    default:
      if (row.merge_blocker) return { at: 'approval', state: 'blocked', note: 'Merge blocked' }
      if (row.approval || actions.includes('assign_reviewer')) return { at: 'approval', state: 'blocked', note: 'Needs a human approval' }
      if (row.environment_blocker) return { at: stoppedAt(row), state: 'blocked', note: 'Environment needs attention' }
      if (row.decision_request) return { at: stoppedAt(row), state: 'blocked', note: 'Needs your decision' }
      if (actions.includes('retry')) return { at: stoppedAt(row), state: 'failed', note: 'Failed' }
      return { at: reached(row, failures > 0), state: 'blocked', note: 'On hold' }
  }
}

export function attemptNote(row: Pick<PipelineRow, 'status' | 'attempts'>): string {
  const attempts = row.attempts
  if (!attempts || !(attempts.failed > 0)) return ''
  if (row.status === 'working' && attempts.failed < attempts.limit) return `attempt ${attempts.failed + 1} of ${attempts.limit}`
  return `attempt ${Math.min(attempts.failed, attempts.limit)} of ${attempts.limit} failed`
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
