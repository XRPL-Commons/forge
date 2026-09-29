export default defineNuxtRouteMiddleware((to) => {
  if (to.path === '/settings/github' && /^#(?:repos|repositories|repo-)/.test(to.hash)) {
    const id = to.hash.match(/^#repo-(\d+)$/)?.[1]
    return navigateTo(id ? `/settings/repositories/${id}` : '/settings/repositories', { replace: true })
  }
})
