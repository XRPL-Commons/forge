import { signInDestination } from '~/utils/signInDestination'

export default defineNuxtRouteMiddleware(async (to) => {
  const auth = useOperator()
  const setupPending = useRuntimeConfig().public.forgeSetupPending
  const login = () => {
    const destination = signInDestination(to.fullPath, setupPending ? '/installation' : '/')
    return navigateTo('/login' + (destination === '/' ? '' : '?return_to=' + encodeURIComponent(destination)), { redirectCode: 303 })
  }
  // The startup hint can outlive setup in an already-open browser. A live
  // session observed by the login page takes precedence over that hint.
  if (setupPending && !auth.session.value?.enabled) {
    if (to.path !== '/login') return login()
    return
  }
  try {
    const session = auth.session.value && Date.now() - auth.checked.value < 30_000
      ? auth.session.value : await auth.refresh()
    if (session.enabled && !session.operator && to.path !== '/login') return login()
    if ((!session.enabled || session.operator) && to.path === '/login') return navigateTo(signInDestination(to.query.return_to, setupPending ? '/installation' : '/'), { redirectCode: 303 })
  } catch {
    // Never render private pages when the authorization service cannot answer.
    if (to.path !== '/login') return login()
  }
})
