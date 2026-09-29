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
  if (!factory) return notAvailable('dispatch', 'Starting new work')
  const enabled = factory.runtime?.enabled !== false
  const detail = !enabled ? factory.operator?.detail : undefined
  return {
    id: 'dispatch', label: 'Starting new work', value: enabled ? 'On' : 'Paused: no new work starts', tone: enabled ? 'ok' : 'warn',
    ...(detail ? { detail } : {}),
    // The anchor is the point: /settings is a long page and the row's link
    // says "Worker capacity", so it lands on that section.
    to: '/settings#worker-capacity', linkLabel: 'Worker capacity',
  }
}

function imageRow(factory: FactoryView | null, onboarding: OnboardingView | null): StatusRow {
  if (!factory || !onboarding) return notAvailable('image', 'Worker environment')
  // The row reports whether the image workers run in passed its check, not
  // whether the onboarding freshness window is still open: a running factory
  // keeps its verdict until the image changes. The digest itself tells the
  // operator nothing, so it is not shown.
  const verified = !!onboarding.image_verified
  return verified
    ? { id: 'image', label: 'Worker environment', value: 'Checked and ready', tone: 'ok' }
    : { id: 'image', label: 'Worker environment', value: 'Not checked since it changed: run the account check', tone: 'warn',
        to: '/settings/harnesses', linkLabel: 'How to check' }
}

function accountsRow(onboarding: OnboardingView | null): StatusRow {
  if (!onboarding) return notAvailable('accounts', 'Coding accounts')
  const entries: { name: string; status: string }[] = []
  for (const harness of onboarding.harnesses ?? []) {
    for (const model of harness.models ?? []) entries.push({ name: harness.name, status: model.status })
  }
  const nonPassed = entries.filter(entry => entry.status !== 'passed')
  if (nonPassed.length === 0) {
    return { id: 'accounts', label: 'Coding accounts', value: 'All signed in and working', tone: 'ok', to: '/settings/harnesses', linkLabel: 'Manage' }
  }
  const value = entries.map(entry => `${entry.name} ${entry.status}`).join(' · ')
  // 'usage limit; retry pending' is self-healing and must not read as an
  // operator task; anything else needing attention (failed, sign-in,
  // verification required, unknown…) is a warn.
  const tone: StatusRow['tone'] = nonPassed.some(entry => entry.status === 'failed' || entry.status === 'verification required')
    ? 'warn'
    : nonPassed.every(entry => entry.status === 'usage limit; retry pending') ? 'muted' : 'warn'
  return { id: 'accounts', label: 'Coding accounts', value, tone, to: '/settings/harnesses', linkLabel: 'Manage' }
}

function githubRow(settings: SettingsSummary | null): StatusRow {
  if (!settings) return notAvailable('github', 'GitHub access')
  const roles = Object.values(settings.roles ?? {})
  const total = roles.length || 4
  const ok = roles.filter(role => role.status === 'ok').length
  return {
    id: 'github', label: 'GitHub access', value: ok < total ? `${ok} of ${total} Forge Apps connected` : 'All Forge Apps connected',
    tone: ok < total ? 'warn' : 'ok', to: '/settings/github', linkLabel: 'Manage',
  }
}

function observationRow(factory: FactoryView | null, now: number): StatusRow {
  if (!factory) return notAvailable('observation', 'Factory process')
  const at = (factory.observation as { at?: number } | undefined)?.at
  if (!at) return { id: 'observation', label: 'Factory process', value: 'Not seen yet', tone: 'warn' }
  const age = Math.max(0, now - at)
  const seen = formatAge(age)
  const when = seen === 'just now' ? seen : `${seen} ago`
  return age > maxAge(factory.runtime)
    ? { id: 'observation', label: 'Factory process', value: `Not responding: last seen ${when}`, tone: 'warn' }
    : { id: 'observation', label: 'Factory process', value: `Running · checked ${when}`, tone: 'ok' }
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

// One sentence for the whole page. Healthy rows say the same thing five
// times, so the page leads with this and folds the rows away unless one
// needs the operator.
export interface StatusSummary { tone: 'ok' | 'warn' | 'muted'; headline: string; attention: StatusRow[] }

export function statusSummary(rows: StatusRow[]): StatusSummary {
  const attention = rows.filter(row => row.tone === 'warn')
  const paused = rows.find(row => row.id === 'dispatch' && row.tone === 'warn')
  if (rows.every(row => row.tone === 'muted')) return { tone: 'muted', headline: 'Status is not available yet.', attention }
  if (!attention.length) return { tone: 'ok', headline: 'Forge is running. Workers can start new work.', attention }
  if (paused && attention.length === 1) return { tone: 'warn', headline: 'Forge is paused. No new work starts.', attention }
  return { tone: 'warn', headline: attention.length === 1 ? 'One thing needs your attention.' : `${attention.length} things need your attention.`, attention }
}
