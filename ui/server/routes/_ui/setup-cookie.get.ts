import { ensureSetupCookie } from '../../utils/setup-cookie'

// A client-side route change into /onboarding, /reset or /settings/harnesses
// never reaches Nitro, so those pages call this on mount. It is a no-op when
// a valid cookie is already there. The `_ui` prefix stays outside
// proxyPaths(), so it is answered here and never forwarded to Python.
export default defineEventHandler((event) => {
  ensureSetupCookie(event)
  setResponseHeader(event, 'cache-control', 'no-store')
  return { ok: true }
})
