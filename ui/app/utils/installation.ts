export interface InstallationStage {
  id: string
  label: string
  status: string
  reason?: string | null
  updated_at?: number | null
  error?: string | null
}
export interface InstallationAction { id: 'continue' | 'retry' | 'connect_account' | 'retry_account'; label: string; proposal_id?: string | number }
export interface InstallationView {
  available: boolean
  generation?: string
  complete: boolean
  started_at?: number | null
  observed_at: number
  updated_at: number | null
  source: 'cli' | 'server'
  state?: string
  reason?: string
  service?: { available: boolean; last_seen: number }
  repositories?: { id: number; name: string }[]
  stages: InstallationStage[]
  preparation?: {
    status: string
    stages: InstallationStage[]
    needs_operator: { repository: { id: number; name: string }; reason: string; path: string }[]
    providers: { id: string; kind: string; connected: boolean }[]
    ready: boolean
  } | null
  account?: { status: string; provider: 'codex'; url?: string; code?: string; expires_at?: number } | null
  smoke: {
    status: string; phase?: string; reason?: string | null; issue_url?: string | null; pull_url?: string | null
    operator_status?: { reason: string; reason_code?: string; label?: string } | null; attempts?: { total: number; charged_failures: number }; proposal_id?: string | number
  } | null
  operation: { status: string; updated_at: number; error?: string | null } | null
  actions: InstallationAction[]
}

export function installationPending(view: InstallationView | null): boolean {
  return Boolean(view?.available && !view.complete)
}
export function installationHeadline(view: InstallationView): string {
  if (view.complete) return 'Your factory is ready'
  if (view.preparation?.needs_operator.length) return 'Waiting for you to finish repository setup'
  if (accountNeedsOperator(view)) return 'Waiting for you to connect Codex'
  if (view.operation?.error) return installationError(view.operation.error)
  const failed = view.stages.find(row => ['failed', 'blocked', 'error'].includes(row.status))
  if (failed) return failed.reason || failed.error || failed.label
  if (view.smoke?.reason) return view.smoke.reason
  const stage = ['failed', 'blocked', 'error', 'running', 'in_progress', 'waiting', 'pending'].map(status => view.stages.find(row => row.status === status)).find(Boolean)
  return stage?.reason || stage?.error || stage?.label || view.reason || 'Setup is in progress'
}
export function stageStatus(status: string): string {
  return ({ complete: 'Complete', completed: 'Complete', passed: 'Complete', running: 'In progress', in_progress: 'In progress', pending: 'Waiting', waiting: 'Waiting', needs_operator: 'Waiting for you', needs_user_action: 'Waiting for you', waiting_for_operator: 'Waiting for you', blocked: 'Needs attention', failed: 'Needs attention', error: 'Needs attention', not_started: 'Not started', skipped: 'Skipped', queued: 'Queued', waiting_to_start: 'Waiting to start', planning: 'Planning the test issue', triage: 'Planning the test issue', verification: 'Checking the factory', build: 'Build', building: 'Building the change', publishing: 'Publishing the pull request', review: 'Review', reviewing: 'Reviewing the change', audit: 'Audit', auditing: 'Auditing the change', merge: 'Merge', merging: 'Merging the pull request', merged: 'Pull request merged', handover: 'Ready to enable repositories' } as Record<string, string>)[status] ?? status.replaceAll('_', ' ')
}
export function elapsedTime(start: number | null | undefined, end: number): string | null {
  if (!start || end < start) return null
  const seconds = Math.floor(end - start)
  if (seconds < 60) return 'less than a minute'
  const minutes = Math.floor(seconds / 60)
  return minutes < 60 ? `${minutes} min` : `${Math.floor(minutes / 60)} hr ${minutes % 60} min`
}
export function githubLink(url: string | null | undefined): string | undefined {
  if (!url) return undefined
  try { const parsed = new URL(url); return parsed.protocol === 'https:' && parsed.hostname === 'github.com' && !parsed.username && !parsed.password ? url : undefined } catch { return undefined }
}

export function installationError(code: string): string {
  return ({ repository_changed: 'The selected repository changed. Check setup before continuing.', readiness_failed: 'A readiness check failed. Check the setup details before retrying.', setup_changed: 'The installation changed. Refresh to load its current status.', smoke_retry_changed: 'The test status changed. Refresh before retrying.', merge_unverified: 'The test merge has not been verified on GitHub.', repository_not_ready: 'A selected repository still needs setup.', repository_policy_changed: 'Repository review rules changed. Check them before continuing.', installation_failed: 'Setup could not continue. Review the current stage and retry.' } as Record<string, string>)[code] ?? code
}

export function accountNeedsOperator(view: InstallationView): boolean {
  return Boolean(view.account?.code || view.actions.some(action => ['connect_account', 'retry_account'].includes(action.id)))
}
export function codexDeviceLink(url: string | undefined): string | undefined {
  if (!url) return undefined
  try { const parsed = new URL(url); return parsed.origin === 'https://auth.openai.com' && parsed.pathname === '/codex/device' && !parsed.username && !parsed.password ? url : undefined } catch { return undefined }
}
export function setupPath(path: string): string | undefined {
  return path.startsWith('/') && !path.startsWith('//') && !path.includes('\\') ? path : undefined
}
