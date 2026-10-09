// Test-side stand-in for the server's `attention_kind` (forge/factory/control.py
// attention_kind), for fixtures written before the field existed. The UI
// never reads the reason prose itself; only these fixtures are translated.
const APPROACH_HOLD = 'Repeated failure requires a supported change of approach'
const PAUSE = /Paused by an operator|Not queued|hold or dependency label/
const ENVIRONMENT = /environment needs attention/

export function attentionKind(item) {
  const state = item.state
  if ((item.blocked_dependencies ?? []).length || state === 'merged' || state === 'cancelled') return null
  if (state === 'approval_needed') return [undefined, null, 'approval'].includes(item.merge_gate_kind) ? 'approve' : 'review'
  if (state === 'failed') return 'failed'
  if (state !== 'hold') return null
  if (item.decision_request) return 'question'
  if (item.reason === APPROACH_HOLD) return 'approach'
  if (item.environment_hold || item.prerequisite_hold || ENVIRONMENT.test(item.reason ?? '')) return 'env'
  if (['pause', 'backlog'].includes(item.operator_hold?.kind) || ['pause', 'backlog'].includes(item.hold_kind) || PAUSE.test(item.reason ?? '')) return null
  return 'review'
}

// Fills in `attention_kind` on every issue row that lacks it.
export function withAttentionKinds(payload) {
  if (!payload || !Array.isArray(payload.issues)) return payload
  if (payload.issues.every(issue => 'attention_kind' in issue)) return payload
  return { ...payload, issues: payload.issues.map(issue => ('attention_kind' in issue ? issue : { ...issue, attention_kind: attentionKind(issue) })) }
}
