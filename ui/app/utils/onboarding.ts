// Pure view logic for guided setup: the Onboarding wizard (`/onboarding`),
// Reset (`/reset`) and Coding accounts (`/settings/harnesses`) all render an
// OnboardingView, and every decision the operator sees — which step opens,
// what step 3 says, what a harness row reads — is taken here so the pages
// stay markup. Copy is verbatim from the operator API contracts §3.7–3.8.

// The contracts' `Role` (GitHub App roles). Not exported, and not named
// `Role`: utils/activity.ts exports a different job-role `Role` and
// utils/repositories.ts exports this same union as `AppRole`, and two
// exports of one name shadow each other in Nuxt's auto-import registry.
type AppRole = 'build' | 'review' | 'audit' | 'controller'
export type SetupPhase = 'setup' | 'legacy' | 'active' | 'resetting'
export type CheckStatus = 'checking' | 'passed' | 'failed'
export type CheckKey = 'github' | 'provider' | 'runner'

export type HarnessStatus = 'connected' | 'passed' | 'failed' | 'unknown'
  | 'sign in again' | 'usage limit; retry pending' | 'verification required'
export interface HarnessModel { id: string; model: string | null; status: HarnessStatus }
export interface Harness {
  id: string; name: string; version: string; connected: boolean
  models: HarnessModel[]; login_command: string; check_command: string
}

export interface SetupActivity {
  target: 'setup' | 'github' | 'provider' | 'runner' | 'start' | 'reset'
  label: string; provider?: string; model_id?: string | null
}

export interface SetupOperation {
  id: string; action: 'check' | 'harness_check' | 'start' | 'reset'
  generation: string; revision: string | null; step: string; at: number
  // Terminal-only: a scoped `setup check <harness>` carries the pool entries it
  // verifies; other accounts keep their verdict and keep dispatching.
  harness?: string; providers?: string[]
  [key: string]: unknown
}

// Local, like AppRole: utils/repositories.ts (Settings · GitHub access and
// Repository setup) exports the contract's fuller versions of these three
// names, and this module only reads the few fields step 3 renders.
interface ProtectionRequirement { id: string; label: string; passed: boolean; detail: string; sources?: unknown[] }
interface ProtectionReport {
  repo_id: number; branch: string | null; observed_at: number; available: boolean
  requirements: ProtectionRequirement[]; [key: string]: unknown
}

interface RepositoryRow {
  id: number; name: string; enabled?: boolean; access_ready?: boolean
  protection_status?: string; protection_ready: boolean
  protection?: ProtectionReport | null; rules_url?: string; setup_url?: string
  [key: string]: unknown
}

export interface RoleState { status: string; registered: boolean; app_id?: number | null; install_url?: string | null }

export interface OnboardingView {
  ok: true; controls_token: string
  phase: SetupPhase; generation: string; revision: string
  account_type?: 'organization' | 'personal' | null
  reset_receipt?: { id: string; generation: string } | null
  roles: Record<AppRole, RoleState>
  repositories: RepositoryRow[]
  selected: { id: number; name: string } | null
  owner: string; provider_connected: boolean
  checks: Partial<Record<CheckKey, CheckStatus>>
  harnesses?: Harness[]
  activity?: SetupActivity | null
  repository_check?: ProtectionReport | null
  repository_setup?: RepositoryRow | null
  qualification_fresh?: boolean
  // True while the running image is the one the last passing check covered;
  // unlike qualification freshness it does not expire.
  image_verified?: boolean
  service?: { last_seen: number; available: boolean }
  can_start: boolean
  operation: SetupOperation | null
  error: string | null
}

export const ROLES: AppRole[] = ['build', 'review', 'audit', 'controller']
export const ROLE_NAMES: Record<AppRole, string> = {
  build: 'Builder', review: 'Reviewer', audit: 'Auditor', controller: 'Controller',
}
export const STEP_LABELS = ['GitHub', 'Repository', 'Start']

// The reset-in-progress screen, shared by /onboarding and /reset.
export const RESETTING_SCREEN = {
  eyebrow: 'Reset in progress',
  title: 'Resetting Forge',
  intro: 'New work is blocked while Forge stops and cleans up its resources.',
  note: 'Reset continues if you close this page. Fresh setup becomes available after cleanup is verified.',
}

// Rendered next to step 3's `secondary` link; the pair is fixed (§3.8
// "Failed-request links").
export const PROTECTION_HELP_LINK = {
  label: 'Repository protection instructions',
  to: '/setup-help#github-repository-protection',
}

export function allRolesOk(view: OnboardingView): boolean {
  return ROLES.every(role => view.roles?.[role]?.status === 'ok')
}

export function nextRole(view: OnboardingView): AppRole | null {
  return ROLES.find(role => view.roles?.[role]?.status !== 'ok') ?? null
}

export function initialStep(view: OnboardingView): 0 | 1 | 2 {
  if (view.selected) return 2
  return allRolesOk(view) ? 1 : 0
}

function fullyVerified(harness: Harness): boolean {
  const models = harness.models ?? []
  return harness.connected && models.length > 0 && models.every(model => model.status === 'passed')
}

function modelSuffix(status: HarnessStatus): string {
  if (status === 'passed') return 'Verified'
  if (status === 'failed') return 'Verification failed'
  return 'Needs verification'
}

// One line per model while any of them still needs attention; a fully
// verified harness names its models alone (§3.7).
export function harnessLine(harness: Harness): string {
  if (!harness.connected) return 'Not connected'
  const models = harness.models ?? []
  const verified = fullyVerified(harness)
  const lines = models.map((model) => {
    const name = model.model || 'Default model'
    return verified ? name : `${name} · ${modelSuffix(model.status)}`
  })
  return lines.join('\n') || 'Needs verification'
}

export function harnessBusy(harness: Harness, activity: SetupActivity | null | undefined): boolean {
  return activity?.target === 'provider' && activity.provider === harness.id
}

export function harnessState(
  harness: Harness, activity: SetupActivity | null | undefined,
): 'verified' | 'verify' | 'connect' | 'busy' {
  if (harnessBusy(harness, activity)) return 'busy'
  if (fullyVerified(harness)) return 'verified'
  return harness.connected ? 'verify' : 'connect'
}

// Step 3's account summary is coarser than the Coding accounts page: one
// passing model is enough to call the harness verified (§3.8 "Coding
// accounts" summary).
export function harnessSummary(harness: Harness, activity: SetupActivity | null | undefined): string {
  if (harnessBusy(harness, activity)) return activity!.label
  if (summaryVerified(harness)) return 'Verified'
  return harness.connected ? 'Needs verification' : 'Not connected'
}

export function summaryVerified(harness: Harness): boolean {
  return harness.connected && (harness.models ?? []).some(model => model.status === 'passed')
}

export function waitingService(view: OnboardingView, now: number): boolean {
  const operation = view.operation
  if (!operation || !view.service || view.service.available) return false
  return now - (operation.at || now) > 15
}

export interface Step3Action {
  label: string
  action: 'check' | 'start' | 'link'
  to?: string
  help?: string
}

export interface Step3View {
  title: string
  intro: string
  // The checking state has no button: either the running activity or the
  // "leave and return" line takes the action's place.
  status?: { label: string; busy: boolean }
  primary?: Step3Action
  secondary?: Step3Action
}

function activityLabel(view: OnboardingView, target: SetupActivity['target']): string | null {
  return view.activity?.target === target ? view.activity.label : null
}

export function step3(view: OnboardingView, now: number): Step3View {
  const checking = Boolean(view.operation)
  const ready = view.can_start
  const connected = view.provider_connected
  const needsRules = Boolean(view.repository_setup) && !view.repository_setup!.protection_ready
  const waiting = waitingService(view, now)
  const failed = Boolean(view.error)
  const inlineReport = Boolean(view.repository_check) && view.checks?.github === 'failed'
  const expired = !ready && !checking && !failed && view.checks?.github === 'passed'

  const title = waiting ? 'Waiting for the Forge service'
    : checking ? (view.operation!.action === 'start' ? 'Starting Forge' : 'Verifying setup')
      : ready ? 'Ready to start Forge'
        : needsRules ? 'Prepare your repository'
          : !connected ? 'Connect a coding account'
            : failed ? 'Setup needs attention'
              : 'Verify your repository'

  const intro = waiting ? 'Your setup is saved. The service has not picked up this request yet. Check that the Forge supervisor is running on the server.'
    : checking ? 'Forge is checking access, coding accounts and worker cleanup. This does not start issue processing.'
      : ready ? 'Verification passed. You decide when issue processing begins.'
        : needsRules ? 'Check the review requirements and download a ready-to-import ruleset if anything is missing.'
          : !connected ? 'Connect at least one coding account before verifying this repository.'
            : failed ? (inlineReport
              ? 'Update the repository settings listed below, then check again. Your coding accounts do not need reconnecting.'
              : 'Verification did not finish. Resolve the error above, then check again.')
              : expired ? 'The previous verification has expired or setup changed. Verify again before starting.'
                : 'Your coding accounts are connected. Repository verification is still required before starting.'

  const help = ready ? 'Starts processing eligible ready issues. Forge creates PRs and merges them when required checks and approvals pass.'
    : needsRules ? 'You apply the rules on GitHub. Forge only reads them.'
      : connected ? (view.qualification_fresh
        ? 'Rechecks repository access and protection using your recent account and worker verification.'
        : 'Verifies accounts and worker isolation. This includes small model requests that may incur provider charges.')
        : ''

  const result: Step3View = { title, intro }
  const withHelp = (action: Step3Action): Step3Action => (help ? { ...action, help } : action)

  if (waiting) {
    result.primary = withHelp({ label: 'Check service setup →', action: 'link', to: '/setup-help#host-installation' })
  } else if (checking) {
    const running = activityLabel(view, 'setup') ?? activityLabel(view, 'start')
    result.status = running
      ? { label: running, busy: true }
      : { label: 'Verification in progress… You can leave and return later.', busy: false }
  } else if (ready) {
    result.primary = withHelp({ label: 'Start Forge', action: 'start' })
  } else if (needsRules) {
    result.primary = withHelp({ label: 'Prepare repository →', action: 'link', to: `/settings/repository?repo_id=${view.selected?.id}` })
  } else if (connected) {
    result.primary = withHelp({ label: failed ? 'Check again' : 'Verify repository', action: 'check' })
  } else {
    result.primary = withHelp({ label: 'Connect a coding account', action: 'link', to: '/settings/harnesses' })
  }

  // A failure the repository panel already explains needs no extra links.
  if (failed && !view.repository_check) {
    result.secondary = { label: 'Check GitHub access', action: 'link', to: '/settings/github' }
  }
  return result
}

export const CHECK_LABELS: Record<CheckKey, string> = {
  github: 'Repository permissions and protection',
  provider: 'Coding account connections and isolation',
  runner: 'Worker stop, cleanup and reuse',
}

export interface CheckRow { id: CheckKey; label: string; value: string; busy: boolean }

export function technicalChecks(view: OnboardingView): CheckRow[] {
  return (Object.keys(CHECK_LABELS) as CheckKey[]).map((id) => {
    const running = activityLabel(view, id)
    if (running) return { id, label: CHECK_LABELS[id], value: running, busy: true }
    const status = view.checks?.[id]
    const value = status === 'checking'
      ? (view.operation ? 'Waiting' : 'Incomplete')
      : status || (view.checks?.github === 'failed' ? 'Not run in this attempt' : 'Not checked')
    return { id, label: CHECK_LABELS[id], value, busy: false }
  })
}

export function checksPassed(view: OnboardingView): boolean {
  return !view.operation && view.checks?.provider === 'passed' && view.checks?.runner === 'passed'
}

export interface RepositoryProblem {
  title: string
  branch: string
  to: string
  missing: ProtectionRequirement[]
  passed: ProtectionRequirement[]
  passedSummary: string
}

export function repositoryProblem(view: OnboardingView): RepositoryProblem | null {
  const report = view.repository_check
  if (!report || !view.error || view.checks?.github !== 'failed') return null
  const missing = report.requirements.filter(requirement => !requirement.passed)
  const passed = report.requirements.filter(requirement => requirement.passed)
  return {
    title: report.available
      ? `${missing.length} repository setting${missing.length === 1 ? ' needs attention' : 's need attention'}`
      : 'Repository protection could not be verified',
    branch: `Branch: ${report.branch}`,
    to: `/settings/repository?repo_id=${view.selected?.id}`,
    missing,
    passed,
    passedSummary: passed.length
      ? `${passed.length} requirement${passed.length === 1 ? ' already passes' : 's already pass'}`
      : '',
  }
}

const RESET_STEPS: Record<string, string> = {
  queued: 'Stopping scheduling',
  stop_workers: 'Stopping workers and verifying cleanup',
  clear_setup: 'Archiving history and clearing setup',
}

export function resetStep(operation: SetupOperation | null | undefined): string {
  return RESET_STEPS[operation?.step || 'queued'] ?? 'Verifying reset'
}

// The completion screen replaces /reset once the state is blank again (a
// receipt with nothing selected) or the generation rolled over under this
// page. A reset that reported an error keeps the operator on the notice.
export function resetComplete(view: OnboardingView, resetGeneration: string | null): boolean {
  if (view.phase !== 'setup' || view.error) return false
  const blank = Boolean(view.reset_receipt) && !view.owner && !view.selected && !view.provider_connected
  return blank || Boolean(resetGeneration && resetGeneration !== view.generation)
}

// The render key: everything the page draws, minus the service heartbeat
// that ticks every second, plus the derived flag that heartbeat feeds.
export function signature(view: OnboardingView, now: number = Date.now() / 1000): string {
  return JSON.stringify({
    ...view,
    service: view.service ? { available: view.service.available } : undefined,
    waitingService: waitingService(view, now),
  })
}
