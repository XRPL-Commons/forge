import { describe, it, expect } from 'vitest'
import { signInNotice } from '../../app/utils/signIn'

describe('signInNotice', () => {
  it('greets a plain visit with no notice', () => {
    expect(signInNotice({ unavailable: false })).toBeNull()
  })

  it('maps each state the auth flow produces to one message', () => {
    expect(signInNotice({ unavailable: true, error: 'sign_in' })).toMatchObject({ kind: 'unavailable', role: 'alert' })
    expect(signInNotice({ unavailable: false, error: 'sign_in' })).toMatchObject({ kind: 'failed', role: 'alert' })
    expect(signInNotice({ unavailable: false, returnTo: '/settings/agents?code=ABCD-EFGH' })).toMatchObject({ kind: 'return', role: 'status' })
  })

  it('never names an account the server did not send', () => {
    expect(signInNotice({ unavailable: false, error: 'sign_in' })!.text).not.toMatch(/@/)
  })

  it('ignores a return_to the middleware never sends', () => {
    expect(signInNotice({ unavailable: false, returnTo: '/elsewhere' })).toBeNull()
    expect(signInNotice({ unavailable: false, returnTo: ['/settings/agents'] })).toBeNull()
  })
})
