// Pure view logic for Settings · GitHub access (contracts §3.5) and Settings ·
// Repository setup (contracts §3.6). Every sentence the two pages can show is
// either a map in this file or is produced by a function here, so the pages
// only bind values and post actions.

export type AppRole = 'build' | 'review' | 'audit' | 'controller'
export const APP_ROLES: AppRole[] = ['build', 'review', 'audit', 'controller']

export type AccessStatus = 'ok' | 'unknown' | 'not_configured' | 'stale' | 'missing_permissions'
export type RepoAccessStatus = 'ready' | 'missing_access' | 'name_changed' | 'archived' | AccessStatus
export type ProtectionStatus = 'unchecked' | 'checking' | 'unknown' | 'stale' | 'ready' | 'needs_setup' | 'access_needed'

export interface RoleAccess {
  status: AccessStatus
  missing_permissions: string[]
  required_permissions: Record<string, 'read' | 'write'>
  reason: string | null
  observed_at: number | null
  installation_url: string | null
  app_slug: string | null
  install_url?: string | null      // only on RepositoriesView.connections
}

export interface ProtectionSource { kind: 'classic' | 'ruleset'; name: string; url: string; id?: number | null; source_type?: string | null }
export interface ProtectionRequirement { id: 'readable' | 'reviews' | 'stale'; label: string; passed: boolean; detail: string; sources: ProtectionSource[] }
export interface ProtectionReport {
  repo_id: number; branch: string | null; observed_at: number; available: boolean
  requirements: ProtectionRequirement[]
  sources: ProtectionSource[]
  errors: { source?: 'classic' | 'rulesets' | 'reviews'; detail: string }[]
  additional_rules: { type: string; source?: ProtectionSource }[]
  code_owner_reviews: boolean
  bypass: { status: 'operator_review'; detail: string }
  required_approvals?: number
  edit_url?: string
}

export interface RepositoryRow {
  id: number; name: string; enabled: boolean; configured: boolean
  access: Record<AppRole, RepoAccessStatus>
  access_ready: boolean; dispatch_allowed: boolean
  protection_status: ProtectionStatus; protection_ready: boolean
  protection: ProtectionReport | null
  protection_previous_status: 'ready' | 'needs_setup' | null
  protection_last_known: ProtectionReport | null
  rules_url: string; setup_url: string
}

export type SyncState =
  | { status: 'never' }
  | { status: 'running'; started_at: number }
  | { status: 'complete'; finished_at: number }
  | { status: 'interrupted'; started_at: number }

export interface FactoryConnection { app_id: number; private_key_env: string; installation_id?: number; installation_ids?: number[] }
export interface FactoryConfig {
  version: 1; sync_interval_seconds: number; access_max_age_seconds: number
  connections: Partial<Record<AppRole, FactoryConnection>>
  repositories: { id: number; name: string; enabled: boolean }[]
}

export interface SettingsView {
  ok: true; controls_token: string
  config: FactoryConfig; revision: string; configured: boolean; config_file: 'factory.json'
  roles: Record<AppRole, RoleAccess>
  repositories: RepositoryRow[]
  sync: SyncState
}

export interface RepositoriesView {
  ok: true; controls_token: string; revision: string
  repositories: RepositoryRow[]
  connections: Record<AppRole, RoleAccess>
  sync: SyncState
  activation_available: boolean
  continue_url: string
}

export interface OnboardingRole { status?: string; registered?: boolean; app_id?: number | null; install_url?: string | null }
export interface OnboardingSummary { phase: string; roles?: Partial<Record<AppRole, OnboardingRole>> }

export const ROLE_LABELS_APP: Record<AppRole, string> = {
  build: 'Builder', review: 'Reviewer', audit: 'Auditor', controller: 'Controller',
}

export const ROLE_PURPOSES: Record<AppRole, string> = {
  build: 'Writes code and opens pull requests.',
  review: 'Reviews proposed changes.',
  audit: 'Checks changes for security issues.',
  controller: 'Coordinates issues and merges approved pull requests.',
}

// Badge text on GitHub access (§3.5). Keys cover both RoleAccess.status and
// the per-repository access values, which share this vocabulary.
export const STATUS_NAMES: Record<string, string> = {
  ready: 'Access verified', ok: 'Connected', unknown: 'Access unknown', not_configured: 'Not connected',
  stale: 'Refresh needed', missing_permissions: 'Permissions missing', missing_access: 'No access',
  name_changed: 'Repository renamed', archived: 'Archived or disabled',
}
export const UNVERIFIED_INSTALLATION = 'Installation unverified'

// Per-role sentences in the repository setup App access panel (§3.6).
export const ACCESS_LABELS: Record<string, string> = {
  missing_access: 'Needs repository access', not_configured: 'Not connected',
  missing_permissions: 'Permissions need updating', unknown: 'Refresh to check access',
  stale: 'Refresh to check access', archived: 'Repository is archived or disabled',
  name_changed: 'Repository was renamed',
}
export const ACCESS_FALLBACK = 'Choose the repository in GitHub’s access settings.'

// Status pill on repository setup (§3.6).
export const PROTECTION_LABELS: Record<ProtectionStatus, string> = {
  ready: 'Ready to enable', needs_setup: 'Setup needed', unknown: 'Check unavailable',
  unchecked: 'Checking setup…', stale: 'Check setup again', checking: 'Checking setup…',
  access_needed: 'App access needed',
}

const INSTALLATION_URL = /^https:\/\/github\.com\/(?:organizations\/[A-Za-z0-9_.-]+\/)?settings\/installations\/[0-9]+$/
const INSTALL_URL = /^https:\/\/github\.com\/apps\/([A-Za-z0-9-]+)\/installations\/new$/

export interface ConnectionLink { label: string; href: string; external: boolean }

// A re-check keeps the previous verdict on screen: `checking` is not a state
// the operator has to act on, so every status-derived sentence reads through
// this.
export function displayStatus(row: RepositoryRow | null | undefined): ProtectionStatus | undefined {
  if (!row) return undefined
  return row.protection_status === 'checking' ? (row.protection_previous_status ?? 'checking') : row.protection_status
}

// The report to render: the fresh one when GitHub answered, else the last
// known one, so a failed check still shows what was verified before.
export function protectionReport(row: RepositoryRow | null | undefined): ProtectionReport | null {
  if (!row) return null
  return (row.protection?.available ? row.protection : row.protection_last_known ?? row.protection) ?? null
}

export function repoStage(row: RepositoryRow | null | undefined, activationAvailable: boolean): 0 | 1 | 2 | 3 | 4 {
  if (!row) return 0
  if (!row.access_ready) return 1
  if (displayStatus(row) !== 'ready') return 2
  return row.enabled && activationAvailable ? 4 : 3
}

export function rowState(row: RepositoryRow): { icon: 'attention' | 'enabled' | 'off'; label: string } {
  const ready = row.protection_ready || (row.protection_status === 'checking' && row.protection_previous_status === 'ready')
  if (row.enabled && (!row.access_ready || !ready)) return { icon: 'attention', label: 'Needs attention' }
  if (row.enabled) return { icon: 'enabled', label: 'Enabled' }
  return { icon: 'off', label: 'Not enabled' }
}

export function repoHeading(row: RepositoryRow | null | undefined, live: boolean): string {
  if (!live) return 'Prepare your repository'
  return row?.enabled ? 'Repository setup' : 'Enable repository'
}

export function statusPillText(row: RepositoryRow, live: boolean): string {
  const shown = displayStatus(row)
  const base = shown === 'ready'
    ? (live ? (row.enabled ? 'Enabled' : 'Ready to enable') : 'Ready to continue')
    : (PROTECTION_LABELS[shown as ProtectionStatus] ?? PROTECTION_LABELS.unknown)
  const refreshing = row.protection_status === 'checking' && row.protection_previous_status
  return refreshing ? base + ' · Refreshing…' : base
}

export function repoDescription(row: RepositoryRow, live: boolean): string {
  if (displayStatus(row) === 'ready') {
    if (live && !row.enabled) return 'Setup is complete. Enable automation to let Forge pick up eligible issues in this repository.'
    if (live) return 'Setup is complete and automation is enabled. No action is needed here.'
    return 'Repository setup is complete. Continue to finish setting up Forge.'
  }
  if (row.protection_status === 'needs_setup') return 'Update the GitHub review rules below, then check again.'
  if (row.protection_status === 'access_needed') return 'Connect the missing Apps before continuing.'
  return 'Forge checks App access and GitHub review rules before enabling automation.'
}

export function hintText(row: RepositoryRow): string {
  const status = row.protection_status
  if (status === 'access_needed') return 'Finish connecting the Apps above to continue.'
  if (status === 'stale') return 'Check again to confirm the current GitHub settings before continuing.'
  if (status === 'unknown') {
    const detail = row.protection?.errors?.[0]?.detail ?? 'GitHub could not be reached. Try checking again.'
    return detail + (row.protection_last_known ? ' Showing the last checked requirements.' : '')
  }
  if (status === 'checking') return 'This usually takes a few seconds. Automation stays unchanged.'
  return ''
}

// Only server-generated, canonical GitHub links are actionable. `registration`
// carries the App id Forge actually holds credentials for (factory.json
// `connections`), not merely a registered App, plus the onboarding install URL.
export function connectionAction(
  role: AppRole,
  roleAccess: RoleAccess,
  registration: OnboardingRole | null | undefined,
): ConnectionLink | null {
  void role
  const manage = roleAccess.installation_url ?? ''
  if (INSTALLATION_URL.test(manage)) return { label: 'Manage access ↗', href: manage, external: true }
  if (registration?.app_id) return null
  const install = registration?.install_url ?? ''
  if (INSTALL_URL.test(install)) return { label: 'Install App ↗', href: install, external: true }
  return { label: 'Connect in setup →', href: '/onboarding', external: false }
}

// The repository setup panel offers the same three links with its own last
// label (§3.6) and reads the install URL off the connection itself.
export function repoAccessAction(connection: RoleAccess | undefined): ConnectionLink {
  const manage = connection?.installation_url ?? ''
  if (INSTALLATION_URL.test(manage)) return { label: 'Manage access ↗', href: manage, external: true }
  const install = connection?.install_url ?? ''
  if (INSTALL_URL.test(install)) return { label: 'Install App ↗', href: install, external: true }
  return { label: 'Connect App →', href: '/onboarding', external: false }
}

export function lastRefreshText(settings: SettingsView): string {
  const sync = settings.sync
  const finished = sync.status === 'complete' ? sync.finished_at : 0
  const observed = Math.max(0, ...APP_ROLES.map(role => settings.roles[role]?.observed_at ?? 0))
  const checked = finished || observed
  const base = checked ? 'Last refresh ' + new Date(checked * 1000).toLocaleString() : 'Access has not been refreshed yet.'
  return base + (sync.status === 'interrupted' ? ' · Refresh interrupted; the previous data is shown.' : '')
}

export function checkedAtText(report: ProtectionReport | null | undefined): string {
  if (!report?.observed_at) return ''
  return 'Last checked ' + new Date(report.observed_at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

export interface ConnectionCardModel {
  role: AppRole; label: string; purpose: string; identity: string
  status: string; ready: boolean; observation: string
  action: ConnectionLink | null; errors: string[]
}

export function connectionCards(settings: SettingsView, onboarding: OnboardingSummary | null | undefined): ConnectionCardModel[] {
  return APP_ROLES.map((role) => {
    const roleAccess = settings.roles[role]
    const connection = settings.config.connections?.[role]
    const registered = onboarding?.roles?.[role]
    const slug = roleAccess.app_slug || INSTALL_URL.exec(registered?.install_url ?? '')?.[1]
    // Credentials, not registration: an App can exist on GitHub while
    // factory.json holds no app_id for it, which is what "unverified" means.
    const unverified = !connection?.app_id && !!registered?.registered
    const count = settings.repositories.filter(repo => repo.access?.[role] === 'ready').length
    const observation = roleAccess.status === 'ok'
      ? `${count} ${count === 1 ? 'repository' : 'repositories'} with verified access`
      : roleAccess.observed_at
        ? 'Last checked ' + new Date(roleAccess.observed_at * 1000).toLocaleString()
        : 'Access has not been verified.'
    return {
      role,
      label: ROLE_LABELS_APP[role],
      purpose: ROLE_PURPOSES[role],
      identity: slug || 'No App connected',
      status: unverified ? UNVERIFIED_INSTALLATION : (STATUS_NAMES[roleAccess.status] ?? roleAccess.status),
      ready: !unverified && ['ok', 'ready'].includes(roleAccess.status),
      observation,
      action: connectionAction(role, roleAccess, { ...registered, app_id: connection?.app_id ?? null }),
      errors: [
        ...(roleAccess.reason ? [roleAccess.reason] : []),
        ...(roleAccess.missing_permissions?.length ? ['Missing ' + roleAccess.missing_permissions.join(', ')] : []),
      ],
    }
  })
}

export interface RepositoryRowModel {
  id: number; name: string; to: string
  state: { icon: 'attention' | 'enabled' | 'off'; label: string }
  note: string
  action: 'continue' | 'disable' | 'setup'
  resolve: string
}

export function repositoryRows(rows: RepositoryRow[], settingUp: boolean): RepositoryRowModel[] {
  return rows.map((row) => {
    const state = rowState(row)
    const needsAttention = state.icon === 'attention'
    const to = '/settings/repository?repo_id=' + row.id
    return {
      id: row.id,
      name: row.name,
      to,
      state,
      note: needsAttention ? 'Needs attention' : '',
      action: settingUp ? 'continue' : row.enabled ? 'disable' : 'setup',
      resolve: !settingUp && row.enabled && needsAttention ? to : '',
    }
  })
}

export interface AccessRowModel { role: AppRole; label: string; ready: boolean; message: string; action: ConnectionLink }

export function accessRows(connections: Partial<Record<AppRole, RoleAccess>> | undefined, row: RepositoryRow | null): AccessRowModel[] {
  return APP_ROLES.map((role) => {
    const connection = connections?.[role]
    const status = row?.access?.[role] ?? connection?.status
    return {
      role,
      label: ROLE_LABELS_APP[role],
      ready: row ? status === 'ready' : false,
      message: ACCESS_LABELS[status as string] ?? ACCESS_FALLBACK,
      action: repoAccessAction(connection),
    }
  })
}

export function scopeText(report: ProtectionReport | null): string {
  return `Applies to every pull request targeting ${report?.branch || 'the default branch'}. Existing organization rules and stronger protections still apply.`
}

export function branchText(report: ProtectionReport | null): string {
  return report?.branch ? 'Default branch · ' + report.branch : 'Repository review settings'
}

export function otherRulesText(report: ProtectionReport | null): string {
  const types = [...new Set((report?.additional_rules ?? []).map(rule => rule.type.replaceAll('_', ' ')))]
  return types.length ? 'GitHub also enforces ' + types.join(', ') + '.' : 'GitHub applies any additional repository protections at merge time.'
}

// `readable` is the "could GitHub be read at all" probe, reported through the
// hint and the errors, never as a review requirement (§3.6).
export function requirements(report: ProtectionReport | null): ProtectionRequirement[] {
  return (report?.requirements ?? []).filter(requirement => requirement.id !== 'readable')
}

export function rulesetHref(row: RepositoryRow, revision: string): string {
  return `/api/repositories/ruleset?repo_id=${row.id}&revision=${encodeURIComponent(revision)}`
}

export function rulesetFilename(row: RepositoryRow): string {
  return `forge-review-rules-${row.id}.json`
}

// `useForgeApi` substitutes its own generic sentence when the API answered
// without a reason; in that case the caller's own sentence is the one the
// operator should read.
const GENERIC_FAILURES = ['', 'Request failed', 'Forge did not accept the action.']
export function actionMessage(error: unknown, fallback: string): string {
  const message = error instanceof Error ? error.message : ''
  return GENERIC_FAILURES.includes(message) ? fallback : message
}
