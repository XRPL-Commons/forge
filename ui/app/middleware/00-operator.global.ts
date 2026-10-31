export default defineNuxtRouteMiddleware(async (to) => {
  if (useRuntimeConfig().public.forgeSetupPending) {
    if (to.path !== '/login') return navigateTo('/login', { redirectCode: 303 })
    return
  }
  const auth = useOperator()
  try {
    const session = auth.session.value && Date.now() - auth.checked.value < 30_000
      ? auth.session.value : await auth.refresh()
    if (session.enabled && !session.operator && to.path !== '/login') return navigateTo(to.path === '/settings/agents' ? '/login?return_to=' + encodeURIComponent(to.fullPath) : '/login', { redirectCode: 303 })
    if ((!session.enabled || session.operator) && to.path === '/login') return navigateTo('/', { redirectCode: 303 })
  } catch {
    // Never render private pages when the authorization service cannot answer.
    if (to.path !== '/login') return navigateTo(to.path === '/settings/agents' ? '/login?return_to=' + encodeURIComponent(to.fullPath) : '/login', { redirectCode: 303 })
  }
})
