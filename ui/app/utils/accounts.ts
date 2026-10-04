// Coding-account health, as `/api/factory` reports it in `coding_accounts`
// (forge/factory/accounts.py). Pure: the global alert bar, the header chip,
// the factory strip and Settings · Coding accounts all word an account the
// same way from here.

export type AccountStatus = 'ok' | 'sign_in_expired' | 'quota' | 'unavailable'

export interface CodingAccount {
  provider: string
  name?: string
  ready: boolean
  status: AccountStatus
  // Unix seconds: when Forge first saw the problem, and when it next retries.
  since: number | null
  until: number | null
  reason_code: string | null
  // A finer cause, e.g. `login_access_only`: the saved login cannot renew.
  detail?: string | null
  waiting_issues: number
}

export interface AccountAlert {
  provider: string
  status: Exclude<AccountStatus, 'ok'>
  color: 'error' | 'warning'
  title: string
  description: string
  actionLabel: string
  to: string
}

// Formats a unix time as a short local clock time ("22:25").
export type ClockFormat = (at: number) => string

export function accountName(account: Pick<CodingAccount, 'provider' | 'name'>): string {
  return account.name || account.provider.charAt(0).toUpperCase() + account.provider.slice(1)
}

export function downAccounts(accounts: CodingAccount[] | null | undefined): CodingAccount[] {
  return (accounts ?? []).filter(account => account.status !== 'ok')
}

// The anchor on Settings · Coding accounts where this account is reconnected.
export function accountAnchor(provider: string): string {
  return `account-${provider}`
}

export function accountHref(provider: string): string {
  return `/settings/harnesses#${accountAnchor(provider)}`
}

function waitingSentence(count: number): string {
  if (!count) return ''
  return count === 1 ? '1 issue is waiting on it.' : `${count} issues are waiting on it.`
}

function coverSentence(account: CodingAccount, all: CodingAccount[]): string {
  const covering = all.filter(other => other.provider !== account.provider && other.status === 'ok').map(accountName)
  if (!covering.length) return 'No other coding account can take over, so new work waits.'
  const names = covering.length === 1 ? covering[0] : `${covering.slice(0, -1).join(', ')} and ${covering[covering.length - 1]}`
  return `${names} ${covering.length === 1 ? 'is' : 'are'} covering what ${covering.length === 1 ? 'it' : 'they'} can.`
}

export function accountAlert(account: CodingAccount, all: CodingAccount[], clock: ClockFormat): AccountAlert | null {
  if (account.status === 'ok') return null
  const name = accountName(account)
  const at = account.since ? clock(account.since) : ''
  let title: string
  let lead = ''
  let actionLabel = `Reconnect ${name}`
  let color: AccountAlert['color'] = 'warning'
  if (account.status === 'sign_in_expired') {
    color = 'error'
    if (account.detail === 'login_access_only') {
      title = `${name} is signed out: its saved login can no longer renew.`
      lead = `Reconnect ${name} to resume.`
    } else {
      title = at ? `${name} can't sign in: its login expired at ${at}.` : `${name} can't sign in: its login has expired.`
    }
  } else if (account.status === 'quota') {
    title = at ? `${name} has reached its usage limit (since ${at}).` : `${name} has reached its usage limit.`
    lead = account.until ? `Forge retries it at ${clock(account.until)}.` : 'Forge retries it automatically.'
    actionLabel = `Review ${name}`
  } else {
    title = `${name} isn't verified, so Forge can't use it.`
    actionLabel = `Verify ${name}`
  }
  const description = [lead, waitingSentence(account.waiting_issues), coverSentence(account, all)].filter(Boolean).join(' ')
  return { provider: account.provider, status: account.status, color, title, description, actionLabel, to: accountHref(account.provider) }
}

export function accountAlerts(accounts: CodingAccount[] | null | undefined, clock: ClockFormat): AccountAlert[] {
  const all = accounts ?? []
  // Sign-in problems first: they never fix themselves.
  const order: Record<AccountStatus, number> = { sign_in_expired: 0, unavailable: 1, quota: 2, ok: 3 }
  return downAccounts(all)
    .sort((a, b) => order[a.status] - order[b.status])
    .map(account => accountAlert(account, all, clock))
    .filter((alert): alert is AccountAlert => alert !== null)
}

// What the bar shows. Dismissing remembers this; any change (another account
// failing, a status or cause changing) shows the bar again.
export function accountsSignature(accounts: CodingAccount[] | null | undefined): string {
  return downAccounts(accounts).map(a => `${a.provider}:${a.status}:${a.detail ?? ''}`).sort().join('|')
}

export function accountsPill(accounts: CodingAccount[] | null | undefined): string | null {
  const count = downAccounts(accounts).length
  if (!count) return null
  return count === 1 ? '1 account down' : `${count} accounts down`
}

const SHORT: Record<Exclude<AccountStatus, 'ok'>, string> = {
  sign_in_expired: 'signed out', quota: 'at its usage limit', unavailable: 'unverified',
}

// The factory strip's headline prefix, e.g. "Codex signed out".
export function accountsHeadline(accounts: CodingAccount[] | null | undefined): string | null {
  const down = downAccounts(accounts)
  if (!down.length) return null
  return down.map(account => `${accountName(account)} ${SHORT[account.status as Exclude<AccountStatus, 'ok'>]}`).join(', ')
}
