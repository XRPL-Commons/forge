import { describe, it, expect } from 'vitest'
import {
  accountAlert, accountAlerts, accountHref, accountsHeadline, accountsPill, accountsSignature, downAccounts, type CodingAccount,
} from '../../app/utils/accounts'

const clock = (at: number) => new Date(at * 1000).toISOString().slice(11, 16)
const SINCE = Date.UTC(2026, 9, 4, 22, 25) / 1000

function account(provider: string, status: CodingAccount['status'], extra: Partial<CodingAccount> = {}): CodingAccount {
  return { provider, name: provider === 'claude' ? 'Claude' : undefined, ready: status === 'ok', status,
           since: null, until: null, reason_code: null, waiting_issues: 0, ...extra }
}

describe('coding account alerts', () => {
  it('says exactly what is wrong with an expired sign-in and where to reconnect', () => {
    const all = [account('codex', 'sign_in_expired', { since: SINCE, waiting_issues: 5, reason_code: 'blocked_auth' }), account('claude', 'ok')]
    expect(accountAlert(all[0]!, all, clock)).toEqual({
      provider: 'codex', status: 'sign_in_expired', color: 'error',
      title: "Codex can't sign in: its login expired at 22:25.",
      description: '5 issues are waiting on it. Claude is covering what it can.',
      actionLabel: 'Reconnect Codex', to: '/settings/harnesses#account-codex',
    })
  })

  it('words a login that can no longer renew', () => {
    const all = [account('codex', 'sign_in_expired', { detail: 'login_access_only', waiting_issues: 1 })]
    const alert = accountAlert(all[0]!, all, clock)!
    expect(alert.title).toBe('Codex is signed out: its saved login can no longer renew.')
    expect(alert.description).toBe('Reconnect Codex to resume. 1 issue is waiting on it. No other coding account can take over, so new work waits.')
  })

  it('only mentions a usage limit for quota, in the warning colour', () => {
    const quota = accountAlert(account('codex', 'quota', { since: SINCE, until: SINCE + 600 }), [], clock)!
    expect(quota.color).toBe('warning')
    expect(quota.title).toContain('usage limit')
    expect(quota.description).toContain('Forge retries it at 22:35.')
    const signedOut = accountAlert(account('codex', 'sign_in_expired'), [], clock)!
    const unverified = accountAlert(account('codex', 'unavailable'), [], clock)!
    expect(unverified.color).toBe('warning')
    for (const alert of [signedOut, unverified]) expect(`${alert.title} ${alert.description}`).not.toMatch(/usage|limit/i)
  })

  it('shows nothing when every account is fine', () => {
    const all = [account('codex', 'ok'), account('claude', 'ok')]
    expect(accountAlerts(all, clock)).toEqual([])
    expect(accountsPill(all)).toBeNull()
    expect(accountsHeadline(all)).toBeNull()
    expect(accountsSignature(all)).toBe('')
    expect(accountAlerts(undefined, clock)).toEqual([])
  })

  it('puts sign-in problems first and counts accounts down', () => {
    const all = [account('claude', 'quota'), account('codex', 'sign_in_expired')]
    expect(accountAlerts(all, clock).map(a => a.provider)).toEqual(['codex', 'claude'])
    expect(accountsPill(all)).toBe('2 accounts down')
    expect(accountsPill([all[1]!])).toBe('1 account down')
    expect(accountsHeadline([all[1]!])).toBe('Codex signed out')
    expect(downAccounts(all)).toHaveLength(2)
  })

  it('changes its dismissal signature when the status changes or another account fails', () => {
    const one = accountsSignature([account('codex', 'sign_in_expired', { since: 1 }), account('claude', 'ok')])
    expect(accountsSignature([account('codex', 'sign_in_expired', { since: 1, waiting_issues: 9 })])).toBe(one)
    expect(accountsSignature([account('codex', 'quota')])).not.toBe(one)
    expect(accountsSignature([account('codex', 'sign_in_expired'), account('claude', 'quota')])).not.toBe(one)
    expect(accountHref('claude')).toBe('/settings/harnesses#account-claude')
  })
})
