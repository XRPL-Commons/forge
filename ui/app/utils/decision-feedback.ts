import type { IssueActionStatus } from './issues'

export interface DecisionReceipt { request: string | null; previous: string | null; message?: string }
const SUBMITTED_DECISION = 'Decision submitted. Waiting for confirmation.'

// A receipt must not be hidden or cleared by the preceding decision's result.
export function receiptAction(actions: IssueActionStatus[], key: string, receipt: DecisionReceipt | null): IssueActionStatus | null {
  const matches = actions.filter(action => action.key === key)
  if (receipt?.request) return matches.find(action => action.id === receipt.request) ?? null
  const last = matches[matches.length - 1] ?? null
  return receipt && last?.id === receipt.previous ? null : last
}

export function decisionFeedback(action: IssueActionStatus | null, receipt: DecisionReceipt | null, stale: boolean): string {
  if (action?.status === 'pending') return 'Applying your decision…'
  if (stale) return 'Showing the last received issue. Refresh before making a decision.'
  if (action) return action.message?.trim() || (action.status === 'applied' ? 'Decision applied.' : 'Decision rejected. Refresh the issue before trying again.')
  return receipt ? receipt.message || SUBMITTED_DECISION : ''
}
