import { describe, expect, it } from 'vitest'
import { decisionFeedback, receiptAction } from '../../app/utils/decision-feedback'
import type { IssueActionStatus } from '../../app/utils/issues'
const old: IssueActionStatus = { id: 'old', key: '1:2', status: 'applied', message: null }
const fresh: IssueActionStatus = { id: 'new', key: '1:2', status: 'pending', message: null }

describe('decision confirmation', () => {
  it('does not let an older applied action conceal a new receipt', () => {
    const receipt = { request: 'new', previous: 'old' }
    expect(receiptAction([old], '1:2', receipt)).toBeNull()
    expect(decisionFeedback(null, receipt, false)).toBe('Decision submitted. Waiting for confirmation.')
    expect(receiptAction([old, fresh], '1:2', receipt)).toEqual(fresh)
    expect(receiptAction([{ ...fresh, key: '1:3' }], '1:2', receipt)).toBeNull()
  })
  it('handles an older server without a request identity without matching the preceding action', () => {
    const receipt = { request: null, previous: 'old' }
    expect(receiptAction([old], '1:2', receipt)).toBeNull()
    expect(receiptAction([old, fresh], '1:2', receipt)).toEqual(fresh)
  })
  it('renders truthful terminal feedback even when the server provides no message', () => {
    expect(decisionFeedback({ ...fresh, status: 'applied' }, null, false)).toBe('Decision applied.')
    expect(decisionFeedback({ ...fresh, status: 'rejected' }, null, false)).toMatch(/^Decision rejected/)
    expect(decisionFeedback({ ...fresh, status: 'rejected', message: 'Revision changed' }, null, false)).toBe('Revision changed')
    expect(decisionFeedback(fresh, null, false)).toBe('Applying your decision…')
    expect(decisionFeedback(old, null, true)).toMatch(/^Showing the last received issue/)
    expect(decisionFeedback(null, null, false)).toBe('')
  })
})
