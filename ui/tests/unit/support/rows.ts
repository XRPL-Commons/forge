// Fixture helpers: the fields the server always sends on an Activity row and
// an issue row, filled in from the few a test names.
import type { CanonicalIssueStatus } from '../../../app/utils/operatorStatus'

type Code = CanonicalIssueStatus['code']
const LABELS: Record<Code, string> = {
  backlog: 'Backlog', queued: 'Queued', working: 'Working', attention: 'Needs attention',
  waiting: 'Waiting', blocked: 'Blocked', completed: 'Completed', dropped: 'No longer needed',
}

export function canonical(code: Code, reason = '', reason_code: string | null = null): CanonicalIssueStatus {
  return { code, label: LABELS[code], reason_code, reason }
}

// The status a test's ledger state stands for, unless the test names one.
const FROM_STATE: Record<string, Code> = {
  ready: 'queued', built: 'queued', reviewed: 'queued', audited: 'queued',
  approval_needed: 'attention', failed: 'attention', investigating: 'waiting', hold: 'blocked',
  merged: 'completed', cancelled: 'dropped',
}

function ledgerCode(row: Record<string, any>): Code {
  if (row.state === 'merged') return 'completed'
  if (row.closed_as === 'completed') return 'completed'
  if (row.closed_as === 'not_planned' || row.closed_as === 'duplicate') return 'dropped'
  const code = FROM_STATE[row.state] ?? 'waiting'
  if (code !== 'attention' && row.blocked_dependencies?.length) return 'blocked'
  if (code === 'queued' && row.active) return 'working'
  return code
}

// The server's attention code (store_views.py attention_kind) for a test that
// only names a ledger state.
const APPROACH_HOLD = 'Repeated failure requires a supported change of approach'
const PAUSE = /Paused by an operator|Not queued|hold or dependency label/
const ENVIRONMENT = /environment needs attention/

export function attentionKind(item: Record<string, any>): string | null {
  const state = item.state
  if ((item.blocked_dependencies ?? []).length || state === 'merged' || state === 'cancelled') return null
  if (state === 'approval_needed') return [undefined, null, 'approval'].includes(item.merge_gate_kind) ? 'approve' : 'review'
  if (state === 'failed') return 'failed'
  if (state !== 'hold') return null
  if (item.decision_request) return 'question'
  if (item.reason === APPROACH_HOLD) return 'approach'
  if (item.environment_hold || ENVIRONMENT.test(item.reason ?? '')) return 'env'
  if (['pause', 'backlog'].includes(item.operator_hold?.kind) || ['pause', 'backlog'].includes(item.hold_kind) || PAUSE.test(item.reason ?? '')) return null
  return 'review'
}

export function factoryRow<T extends Record<string, any>>(row: T): any {
  return {
    blocked_dependencies: [], closed_as: null, transition: null, attention_kind: null,
    ...row,
    operator_status: row.operator_status ?? canonical(ledgerCode(row), row.reason ?? ''),
  }
}

export function issueRow<T extends Record<string, any>>(row: T): any {
  return { ...row, operator_status: row.operator_status ?? canonical(row.status, row.explanation ?? '') }
}
