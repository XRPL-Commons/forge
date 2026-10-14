import { randomBytes } from 'node:crypto'
import type { H3Event } from 'h3'

// Contracts §1 "Cookie forge_setup": Python sets it on GET /onboarding,
// /reset and /settings/harnesses and reads it back on POST /api/onboarding
// {action:'github'} and on the GitHub callback. Nitro serves those pages, so
// Nitro has to mint the cookie; the proxy forwards it upstream unchanged.
const SETUP_COOKIE = 'forge_setup'
// Python accepts an existing value matching this, else mints a new one.
// base64url of 32 bytes is exactly 43 characters, inside the range.
const VALID = /^[A-Za-z0-9_-]{32,128}$/

export function ensureSetupCookie(event: H3Event): string {
  const current = getCookie(event, SETUP_COOKIE)
  if (current && VALID.test(current)) return current
  const token = randomBytes(32).toString('base64url')
  setCookie(event, SETUP_COOKIE, token, { path: '/', httpOnly: true, sameSite: 'lax', maxAge: 3600 })
  return token
}
