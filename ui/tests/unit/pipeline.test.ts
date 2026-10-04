import { describe, it, expect } from 'vitest'
import { attemptNote, issuePipeline, type PipelineRow } from '../../app/utils/pipeline'

const base: PipelineRow = { status: 'queued', explanation: 'Queued for Forge.', actions: [], pull_requests: [] }
const row = (over: Partial<PipelineRow>): PipelineRow => ({ ...base, ...over })
const candidate = [{ number: 30, title: 'Fix', kind: 'candidate' as const }]
const at = (over: Partial<PipelineRow>) => {
  const view = issuePipeline(row(over))
  return [view.current, view.state, view.note]
}
const states = (over: Partial<PipelineRow>) => issuePipeline(row(over)).steps.map(step => step.state)

describe('issuePipeline', () => {
  it('uses server codes even when prose contradicts them', () => {
    expect(at({ status: 'working', reason_code: 'audit', explanation: 'Building' })[0]).toBe('audit')
    expect(at({ status: 'queued', reason_code: 'waiting_account', ledger_state: 'reviewed', explanation: 'Queued for Forge.' })[0]).toBe('audit')
    expect(at({ status: 'completed', reason_code: 'closed', explanation: 'Completed and merged.', summary: { headline: 'Merged', plain: '' } })[0]).toBeNull()
    expect(at({ status: 'completed', ledger_state: 'merged', explanation: 'Different words' })[0]).toBe('merged')
    expect(at({ status: 'working', reason_code: 'recovery', explanation: 'Different words', pull_requests: candidate })[2]).toBe('Looking into the failure')
  })
  it('lists the six stages in order with compact labels', () => {
    const view = issuePipeline(base)
    expect(view.steps.map(step => step.label)).toEqual(['Queued', 'Build', 'Review', 'Security review', 'Approval', 'Merged'])
    expect(view.steps.map(step => step.short)).toEqual(['Queue', 'Build', 'Review', 'Security', 'Approval', 'Merged'])
  })

  it('places a queued or backlog issue on Queued', () => {
    expect(at({})).toEqual(['queued', 'waiting', 'Waiting'])
    expect(at({ status: 'backlog', explanation: 'Not queued for Forge.', actions: ['queue'] })).toEqual(['queued', 'upcoming', 'Not queued'])
  })

  it('takes an issue that is no longer needed off the line, with nothing earned', () => {
    const view = issuePipeline(row({ status: 'dropped', explanation: 'No longer needed: closed as not planned.', pull_requests: candidate }))
    expect([view.current, view.state, view.note]).toEqual([null, 'closed', 'No longer needed'])
    expect(view.steps.some(step => step.state === 'done')).toBe(false)
  })

  it('waits calmly where it stands when blocked on other issues, never as needing attention', () => {
    expect(at({ status: 'blocked', explanation: 'Waiting for #90.' })).toEqual(['queued', 'waiting', 'Waiting on other issues'])
    expect(at({ status: 'blocked', explanation: 'Waiting for #90.', pull_requests: candidate })).toEqual(['review', 'waiting', 'Waiting on other issues'])
  })

  it('follows the running worker’s role', () => {
    expect(at({ status: 'working', reason_code: 'build', explanation: 'Building' })).toEqual(['build', 'active', 'In progress'])
    expect(at({ status: 'working', reason_code: 'review', explanation: 'Reviewing' })[0]).toBe('review')
    expect(at({ status: 'working', reason_code: 'audit', explanation: 'Security review' })[0]).toBe('audit')
    expect(at({ status: 'working', reason_code: 'merge', explanation: 'Merging' })[0]).toBe('merged')
    expect(states({ status: 'working', reason_code: 'review', explanation: 'Reviewing' })).toEqual(['done', 'done', 'active', 'upcoming', 'upcoming', 'upcoming'])
  })

  it('places an issue between stages on the one it waits for', () => {
    expect(at({ reason_code: 'built', explanation: 'Waiting for review.' })[0]).toBe('review')
    expect(at({ reason_code: 'reviewed', explanation: 'Waiting for security review.' })[0]).toBe('audit')
    expect(at({ reason_code: 'audited', explanation: 'Checking merge requirements.' })[0]).toBe('approval')
    // A coding-account wait replaces the line; the pull request still says how far it got.
    expect(at({ explanation: 'Waiting for a coding account. Forge will retry automatically.', pull_requests: candidate })[0]).toBe('review')
  })

  it('shows a failure on the stage where it happened', () => {
    expect(at({ status: 'attention', actions: ['retry'], explanation: 'The last attempt failed.' })).toEqual(['build', 'failed', 'Failed'])
    expect(at({ status: 'attention', actions: ['retry'], pull_requests: candidate,
      reviews: [{ role: 'review', outcome: 'reject', summary: '' }] })[0]).toBe('review')
    expect(at({ status: 'attention', actions: ['retry'], pull_requests: candidate,
      reviews: [{ role: 'review', outcome: 'pass', summary: '' }, { role: 'audit', outcome: 'unable', summary: '' }] })[0]).toBe('audit')
    expect(states({ status: 'attention', actions: ['retry'], pull_requests: candidate,
      reviews: [{ role: 'review', outcome: 'reject', summary: '' }] })).toEqual(['done', 'done', 'failed', 'upcoming', 'upcoming', 'upcoming'])
  })

  it('shows approvals and merge blockers on Approval', () => {
    expect(at({ status: 'attention', approval: { pr: 31, reason: 'x' }, actions: ['assign_reviewer'] }))
      .toEqual(['approval', 'blocked', 'Needs a human approval'])
    expect(at({ status: 'attention', merge_blocker: { reason: 'Checks failing' } })).toEqual(['approval', 'blocked', 'Merge blocked'])
  })

  it('names environment holds and open decisions where the work stopped', () => {
    expect(at({ status: 'attention', actions: ['recheck'], environment_blocker: 'Docker is down', pull_requests: candidate }))
      .toEqual(['review', 'blocked', 'Environment needs attention'])
    expect(at({ status: 'attention', actions: ['retry'],
      decision_request: { question: 'Which?', options: [], recommendation: '', evidence: '' } }))
      .toEqual(['build', 'blocked', 'Needs your decision'])
  })

  it('keeps a held issue that never started on Queued', () => {
    expect(at({ status: 'attention', actions: ['resume'], explanation: 'Paused by an operator.' })).toEqual(['queued', 'blocked', 'On hold'])
  })

  it('ends on Merged, or off the strip when closed without merging', () => {
    expect(at({ status: 'completed', reason_code: 'merged', explanation: 'Completed and merged.' })).toEqual(['merged', 'done', 'Merged'])
    expect(states({ status: 'completed', reason_code: 'merged', explanation: 'Completed and merged.' }).every(state => state === 'done')).toBe(true)
    expect(at({ status: 'completed', reason_code: 'closed', explanation: 'Closed on GitHub.' })).toEqual([null, 'closed', 'Closed on GitHub'])
    expect(states({ status: 'completed', reason_code: 'closed', explanation: 'Closed on GitHub.' }).every(state => state === 'upcoming')).toBe(true)
  })
})

describe('attemptNote', () => {
  it('counts attempts only once one failed', () => {
    expect(attemptNote({ status: 'queued', attempts: null })).toBe('')
    expect(attemptNote({ status: 'queued', attempts: { failed: 0, limit: 3 } })).toBe('')
    expect(attemptNote({ status: 'attention', attempts: { failed: 3, limit: 3 } })).toBe('attempt 3 of 3 failed')
    expect(attemptNote({ status: 'working', attempts: { failed: 1, limit: 3 } })).toBe('attempt 2 of 3')
  })
})
