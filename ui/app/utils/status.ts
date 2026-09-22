// Pure view logic for Settings · General as the factory status page. Every
// visible surface is produced here from the three independently-fetched
// payloads (GET /api/factory, /api/settings, /api/onboarding); the page never
// reaches into a raw payload itself. Rules follow the operator API contracts
// §2 (GET /api/factory, GET/POST /api/settings, POST /api/runtime,
// GET /api/onboarding) and §3.4 (Settings · General).
import { formatAge } from './format'
import { maxAge, type FactoryView } from './factory'
import type { OnboardingView } from './onboarding'

// Named `*Summary` (not `RoleAccess`/`SettingsView`) because those are the
// contract-level names (operator-api-contracts.md §2's full `RoleAccess` and
// `SettingsView` types) that another page will import in full; statusRows()
// only ever reads `roles[*].status`, so it declares its own minimal local
// shape rather than claim those names for itself.
export interface RoleAccessSummary { status: 'ok' | 'unknown' | 'not_configured' | 'stale' | 'missing_permissions' | string }
export interface SettingsSummary { roles?: Record<string, RoleAccessSummary> }

export interface StatusSources {
  factory: FactoryView | null
  settings: SettingsSummary | null
  onboarding: OnboardingView | null
}

export interface StatusRow {
  id: 'dispatch' | 'image' | 'accounts' | 'github' | 'observation'
  label: string
  value: string
  tone: 'ok' | 'warn' | 'muted'
  detail?: string
  to?: string
  linkLabel?: string
}

function notAvailable(id: StatusRow['id'], label: string): StatusRow {
  return { id, label, value: 'Not available', tone: 'muted' }
}

function dispatchRow(factory: FactoryView | null): StatusRow {
  if (!factory) return notAvailable('dispatch', 'Dispatch')
  const enabled = factory.runtime?.enabled !== false
  const detail = !enabled ? factory.operator?.detail : undefined
  return {
    id: 'dispatch', label: 'Dispatch', value: enabled ? 'Enabled' : 'Paused', tone: enabled ? 'ok' : 'warn',
    ...(detail ? { detail } : {}),
    // The anchor is the point: /settings is a long page and the row's link
    // says "Worker capacity", so it lands on that section.
    to: '/settings#worker-capacity', linkLabel: 'Worker capacity',
  }
}

// The image carries an optional digest suffix ("<repo>:<tag>@sha256:<hex>");
// the tag boundary is the '@' that starts it, never a fixed character count.
function truncateImage(image: string): string {
  const at = image.indexOf('@')
  return at === -1 ? image : image.slice(0, at)
}

function imageRow(factory: FactoryView | null, onboarding: OnboardingView | null): StatusRow {
  if (!factory || !onboarding) return notAvailable('image', 'Image')
  const fresh = !!onboarding.qualification_fresh
  const image = truncateImage(String(factory.runtime?.image ?? ''))
  return {
    id: 'image', label: 'Image',
    value: `${image} · verification ${fresh ? 'current' : 'required'}`,
    tone: fresh ? 'ok' : 'warn',
    to: '/settings/harnesses', linkLabel: 'Coding accounts',
  }
}

function accountsRow(onboarding: OnboardingView | null): StatusRow {
  if (!onboarding) return notAvailable('accounts', 'Accounts')
  const entries: { name: string; status: string }[] = []
  for (const harness of onboarding.harnesses ?? []) {
    for (const model of harness.models ?? []) entries.push({ name: harness.name, status: model.status })
  }
  const nonPassed = entries.filter(entry => entry.status !== 'passed')
  if (nonPassed.length === 0) {
    return { id: 'accounts', label: 'Accounts', value: 'All coding accounts verified.', tone: 'ok', to: '/settings/harnesses', linkLabel: 'Coding accounts' }
  }
  const value = entries.map(entry => `${entry.name} ${entry.status}`).join(' · ')
  // 'usage limit; retry pending' is self-healing and must not read as an
  // operator task; anything else needing attention (failed, sign-in,
  // verification required, unknown…) is a warn.
  const tone: StatusRow['tone'] = nonPassed.some(entry => entry.status === 'failed' || entry.status === 'verification required')
    ? 'warn'
    : nonPassed.every(entry => entry.status === 'usage limit; retry pending') ? 'muted' : 'warn'
  return { id: 'accounts', label: 'Accounts', value, tone, to: '/settings/harnesses', linkLabel: 'Coding accounts' }
}

function githubRow(settings: SettingsSummary | null): StatusRow {
  if (!settings) return notAvailable('github', 'GitHub')
  const roles = Object.values(settings.roles ?? {})
  const total = roles.length || 4
  const ok = roles.filter(role => role.status === 'ok').length
  return {
    id: 'github', label: 'GitHub', value: `${ok} of ${total} Apps verified`, tone: ok < total ? 'warn' : 'ok',
    to: '/settings/github', linkLabel: 'GitHub access',
  }
}

function observationRow(factory: FactoryView | null, now: number): StatusRow {
  if (!factory) return notAvailable('observation', 'Observation')
  const at = (factory.observation as { at?: number } | undefined)?.at
  if (!at) return { id: 'observation', label: 'Observation', value: 'No observation yet', tone: 'warn' }
  const age = now - at
  return {
    id: 'observation', label: 'Observation',
    value: `Supervisor seen ${formatAge(Math.max(0, age))} ago`,
    tone: age > maxAge(factory.runtime) ? 'warn' : 'ok',
  }
}

export function statusRows(sources: StatusSources, now: number): StatusRow[] {
  return [
    dispatchRow(sources.factory),
    imageRow(sources.factory, sources.onboarding),
    accountsRow(sources.onboarding),
    githubRow(sources.settings),
    observationRow(sources.factory, now),
  ]
}
