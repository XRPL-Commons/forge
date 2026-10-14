// Native labels and finer reasons come from one server projection
// (forge/factory/status_projection.py), shared by all issue surfaces.
type OperatorStatusCode = 'backlog' | 'queued' | 'working' | 'attention' | 'waiting' | 'blocked' | 'completed' | 'dropped'
export interface CanonicalIssueStatus {
  code: OperatorStatusCode
  label: string
  reason_code: string | null
  reason: string
  owner?: string
  next_action?: { owner: string; reason: string; due_at: number }
}
export interface OperatorStatusSource { operator_status: CanonicalIssueStatus }

export function operatorReason(row: Partial<OperatorStatusSource> | null | undefined): string | undefined {
  return row?.operator_status?.reason
}
