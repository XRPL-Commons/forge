import type { OnboardingView } from '~/utils/onboarding'

// Route redirects mirroring the Python 303s (contracts §2, "Pages/assets
// served"): an operator who is mid-setup, or mid-reset, is always sent back
// to the one page that can move them forward.
const SETUP_PAGES = ['/onboarding', '/reset', '/setup-help']
// Contracts §1, GET guard 2. /api/onboarding is exempt from the fence, but
// letting the reason surface as a page error would be a dead end anyway.
const RESET_FENCE = 'Factory reset is in progress'

type Payload = OnboardingView | { ok: false; reason: string }

export default defineNuxtRouteMiddleware(async (to) => {
  if (SETUP_PAGES.includes(to.path)) return
  // useRequestFetch so the SSR call carries the browser's Host and Cookie
  // through the proxy to Python.
  const request = useRequestFetch()

  let payload: Payload
  try {
    payload = await request<Payload>('/api/onboarding', { headers: { accept: 'application/json' } })
  } catch {
    // Setup state is unreachable: the page renders its own connection error
    // rather than being bounced somewhere that cannot load either.
    return
  }

  const onboarding = () => navigateTo('/onboarding', { redirectCode: 303 })

  if (!payload || payload.ok === false) {
    return payload?.reason === RESET_FENCE ? onboarding() : undefined
  }
  if (payload.phase === 'resetting') return onboarding()

  const activated = payload.phase === 'active'
  // legacy/forge-status.py: `/` redirects when the factory is not activated
  // and (guided setup is enabled or no repository is configured). Guided
  // setup is the only way in for this UI, so the first half decides it. The
  // payload's `repositories` are the access-ready ones, so reading "no
  // repositories" from them would bounce a healthy factory whose GitHub
  // access briefly degraded.
  if (to.path === '/' && !activated) return onboarding()
  // /settings/advanced is the same page; the other settings tabs stay
  // reachable during setup, because step 3 links into them.
  if ((to.path === '/settings' || to.path === '/settings/advanced') && !activated) return onboarding()
})
