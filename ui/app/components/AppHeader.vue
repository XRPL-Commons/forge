<script setup lang="ts">
import type { DropdownMenuItem } from '@nuxt/ui'
import type { OnboardingView } from '~/utils/onboarding'
const route = useRoute()
const { name, named } = useFactoryIdentity()
const { session, logout } = useOperator()
const logoutError = ref('')
async function signOut() {
  logoutError.value = ''
  try { await logout() } catch { logoutError.value = 'Sign-out failed. Try again.' }
}
const accountItems = computed<DropdownMenuItem[][]>(() => [
  [{ label: session.value?.operator?.login, type: 'label' }],
  [{ label: 'Sign out', icon: 'i-lucide-log-out', onSelect: signOut }],
])
// The setup state the phase middleware already read on this navigation.
// While Forge is not started, Activity and Settings · General redirect to
// guided setup, so the nav offers Setup and GitHub access instead (#73).
const onboarding = useResources().entry<OnboardingView | { ok: false }>('/api/onboarding')
const settingUp = computed(() => {
  const value = onboarding.data.value
  return !!value && 'phase' in value && value.phase !== 'active'
})
const links = computed(() => [
  settingUp.value
    ? { to: '/onboarding', label: 'Setup', current: (path: string) => path === '/onboarding' }
    : { to: '/', label: 'Activity', current: (path: string) => path === '/' },
  { to: '/issues', label: 'Issues', current: (path: string) => path === '/issues' || path.startsWith('/issues/') },
  { to: settingUp.value ? '/settings/github' : '/settings', label: 'Settings', current: (path: string) => path === '/settings' || path.startsWith('/settings/') },
])
</script>
<template>
  <header class="mx-auto flex max-w-[1600px] flex-wrap items-center justify-between gap-x-5 gap-y-2 px-4 py-3 sm:px-7 sm:py-4">
    <NuxtLink :to="settingUp ? '/onboarding' : '/'" :aria-label="`${name} home`" class="flex min-w-0 max-w-full items-center gap-2.5 text-lg font-bold tracking-tight text-white no-underline">
      <span aria-hidden="true" class="grid size-7 shrink-0 -rotate-6 place-items-center rounded-md border border-mint-200 text-mint-200">F</span>
      <span class="min-w-0 break-words [overflow-wrap:anywhere]">
        {{ name }}<span v-if="named" class="ml-2 text-xs font-normal text-muted">Forge</span>
      </span>
    </NuxtLink>
    <div class="flex flex-wrap items-center gap-x-5 gap-y-2">
      <ActivityPill />
      <AccountsPill />
      <nav aria-label="Main navigation" class="flex items-center gap-4 text-sm sm:gap-5">
        <NuxtLink
          v-for="link in links"
          :key="link.label"
          :to="link.to"
          :aria-current="link.current(route.path) ? 'page' : undefined"
          :class="link.current(route.path) ? 'text-mint-200 underline underline-offset-4' : 'text-muted hover:text-white'"
        >{{ link.label }}</NuxtLink>
        <NuxtLink to="/setup-help" aria-label="Help" title="Help" class="inline-flex items-center justify-center text-muted hover:text-white">
          <UIcon name="i-lucide-circle-help" class="size-5" aria-hidden="true" />
        </NuxtLink>
      </nav>
      <UDropdownMenu v-if="session?.operator" :items="accountItems" :content="{ align: 'end' }">
        <UButton
          icon="i-lucide-circle-user-round"
          color="neutral"
          variant="ghost"
          :aria-label="`Account menu for ${session.operator.login}`"
          title="Account"
        />
      </UDropdownMenu>
      <span v-if="logoutError" role="alert" class="text-sm">{{ logoutError }}</span>
    </div>
  </header>
</template>
