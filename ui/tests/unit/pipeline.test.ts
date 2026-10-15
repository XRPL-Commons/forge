import { describe, it, expect } from 'vitest'
import { attemptNote, issuePipeline, type PipelineRow } from '../../app/utils/pipeline'
import { canonical } from './support/rows'

const base: PipelineRow = { status: 'queued', pull_requests: [], operator_status: canonical('queued', 'Scheduled to start', 'admitted') }
const row = (over: Partial<PipelineRow>): PipelineRow => ({ ...base, ...over })
const candidate = [{ number: 30, title: 'Fix', kind: 'candidate' as const }]
const at = (over: Partial<PipelineRow>) => {
  const view = issuePipeline(row(over))
  return [view.current, view.state, view.note]
}
const states = (over: Partial<PipelineRow>) => issuePipeline(row(over)).steps.map(step => step.state)

describe('issuePipeline', () => {
  it('lists the six stages in order with compact labels', () => {
    const view = issuePipeline(base)
    expect(view.steps.map(step => step.label)).toEqual(['Queued', 'Build', 'Review', 'Security review', 'Approval', 'Merged'])
    expect(view.steps.map(step => step.short)).toEqual(['Queue', 'Build', 'Review', 'Security', 'Approval', 'Merged'])
  })

  it('places an issue on the lifecycle stage, with the server’s reason as the note', () => {
    expect(at({ stage_code: 'queued' })).toEqual(['queued', 'waiting', 'Scheduled to start'])
    expect(at({ status: 'working', stage_code: 'building', operator_status: canonical('working', 'Building') })).toEqual(['build', 'active', 'Building'])
    expect(at({ status: 'working', stage_code: 'publishing', operator_status: canonical('working', 'Integrating') })[0]).toBe('build')
    expect(at({ status: 'working', stage_code: 'auditing', operator_status: canonical('working', 'Auditing') })[0]).toBe('audit')
    expect(at({ stage_code: 'merging', operator_status: canonical('waiting', 'Checking merge approval', 'authorization') })).toEqual(['approval', 'waiting', 'Checking merge approval'])
    expect(states({ status: 'working', stage_code: 'reviewing', operator_status: canonical('working', 'Reviewing') }))
      .toEqual(['done', 'done', 'active', 'upcoming', 'upcoming', 'upcoming'])
  })

  it('falls back on the reviews and the pull request when the stage does not place it', () => {
    expect(at({})[0]).toBe('queued')
    expect(at({ pull_requests: candidate })[0]).toBe('review')
    expect(at({ pull_requests: [{ number: 31, title: 'Other', kind: 'external' }] })[0]).toBe('queued')
    expect(at({ pull_requests: candidate, reviews: [{ role: 'review', outcome: 'pass', summary: '' }] })[0]).toBe('audit')
    expect(at({ pull_requests: candidate, reviews: [{ role: 'audit', outcome: 'passed', summary: '' }] })[0]).toBe('approval')
  })

  it('shows an exhausted budget as failed and any other attention as blocked', () => {
    expect(at({ status: 'attention', stage_code: 'reviewing', operator_status: canonical('attention', 'Three attempts failed', 'budget_exhausted') }))
      .toEqual(['review', 'failed', 'Three attempts failed'])
    expect(at({ status: 'attention', stage_code: 'merging', operator_status: canonical('attention', 'Needs a human approval', 'approval_required') }))
      .toEqual(['approval', 'blocked', 'Needs a human approval'])
  })

  it('waits calmly where it stands when blocked on other issues', () => {
    expect(at({ status: 'blocked', stage_code: 'building', operator_status: canonical('blocked', 'Waiting for #90', 'dependency') }))
      .toEqual(['build', 'waiting', 'Waiting for #90'])
  })

  it('keeps a backlog issue on Queued, upcoming', () => {
    expect(at({ status: 'backlog', operator_status: canonical('backlog', 'Not queued for Forge') })).toEqual(['queued', 'upcoming', 'Not queued for Forge'])
  })

  it('takes an issue that is no longer needed off the line, with nothing earned', () => {
    const view = issuePipeline(row({ status: 'dropped', pull_requests: candidate, operator_status: canonical('dropped', 'Closed as not planned', 'github_not_planned') }))
    expect([view.current, view.state, view.note]).toEqual([null, 'closed', 'Closed as not planned'])
    expect(view.steps.some(step => step.state === 'done')).toBe(false)
  })

  it('ends on Merged, or off the strip when completed without a merge', () => {
    const merged = { status: 'completed' as const, operator_status: canonical('completed', 'Merged', 'merged') }
    expect(at(merged)).toEqual(['merged', 'done', 'Merged'])
    expect(states(merged).every(state => state === 'done')).toBe(true)
    const closed = { status: 'completed' as const, operator_status: canonical('completed', 'Completed on GitHub', 'github_completed') }
    expect(at(closed)).toEqual([null, 'done', 'Completed on GitHub'])
    expect(states(closed).every(state => state === 'upcoming')).toBe(true)
  })
})

describe('attemptNote', () => {
  it('counts attempts only once one failed', () => {
    expect(attemptNote({ status: 'queued', attempts: null })).toBe('')
    expect(attemptNote({ status: 'queued', attempts: { failed: 0, limit: 3 } })).toBe('')
    expect(attemptNote({ status: 'attention', attempts: { failed: 3, limit: 3 } })).toBe('Attempt 3 of 3 failed.')
    expect(attemptNote({ status: 'working', attempts: { failed: 1, limit: 3 } })).toBe('Attempt 2 of 3 is running.')
  })
})
