import { ensureSetupCookie } from '../utils/setup-cookie'

// Every GET that is not an API call is a page or an asset the operator's
// browser is loading, so it is a chance to make sure the setup cookie
// exists before any onboarding POST needs it. /api/** is skipped: those
// requests are proxied to Python, which owns the cookie it already sees
// (and /onboarding/github/callback is answered by the proxy middleware,
// which runs first, so its Set-Cookie is never overwritten here).
export default defineEventHandler((event) => {
  if ((event.node.req.method ?? 'GET') !== 'GET') return
  const path = event.path.split('?')[0]
  if (path === '/api' || path.startsWith('/api/')) return
  ensureSetupCookie(event)
})
