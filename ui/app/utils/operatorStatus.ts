// Native labels and finer reasons come from one server projection. The fallback
// supports pre-cutover servers; it is shared by all issue surfaces too.
export type OperatorStatusCode = 'backlog' | 'queued' | 'working' | 'attention' | 'waiting' | 'blocked' | 'completed' | 'dropped'
export interface CanonicalIssueStatus {
  code: OperatorStatusCode
  label: string
  reason_code: string | null
  reason: string
  owner?: string
  next_action?: { owner: string; reason: string; due_at: number }
}
export interface OperatorStatusSource { operator_status?: CanonicalIssueStatus }

// Used only when the server has not supplied operator_status.
const LEGACY_LABELS: Record<OperatorStatusCode, string> = {
  backlog: 'Backlog', queued: 'Queued', working: 'Working', attention: 'Needs attention',
  waiting: 'Waiting', blocked: 'Blocked', completed: 'Completed', dropped: 'No longer needed',
}
const LEGACY_STATES: Record<string, OperatorStatusCode> = {
  ready: 'queued', built: 'queued', reviewed: 'queued', audited: 'queued',
  approval_needed: 'attention', failed: 'attention', investigating: 'waiting', hold: 'blocked',
  merged: 'completed', cancelled: 'dropped',
}
export type StatusSource = OperatorStatusSource & {
  status?: string; state?: string; closed_as?: string | null; active?: string | null
  blocked_dependencies?: number[]; ownership_wait?: unknown
}

export function operatorLabel(row: StatusSource | null | undefined): string {
  if (row?.operator_status) return row.operator_status.label
  let code = row?.status as OperatorStatusCode | undefined
  if (!code || !(code in LEGACY_LABELS)) {
    code = LEGACY_STATES[row?.state ?? ''] ?? 'waiting'
    if (row?.state === 'merged') code = 'completed'
    else if (row?.closed_as === 'completed') code = 'completed'
    else if (row?.closed_as === 'not_planned' || row?.closed_as === 'duplicate') code = 'dropped'
    else if (code !== 'attention' && (row?.ownership_wait || row?.blocked_dependencies?.length)) code = 'blocked'
    else if (code === 'queued' && row?.active) code = 'working'
  }
  return LEGACY_LABELS[code]
}

export function operatorReason(row: OperatorStatusSource | null | undefined): string | undefined {
  return row?.operator_status?.reason
}
