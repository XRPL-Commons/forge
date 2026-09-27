<script setup lang="ts">
const route = useRoute()
// One top-level nav on every page; Settings keeps its own second row of tabs.
const links = [
  { to: '/', label: 'Activity', current: (path: string) => path === '/' },
  { to: '/issues', label: 'Issues', current: (path: string) => path === '/issues' || path.startsWith('/issues/') },
  { to: '/settings', label: 'Settings', current: (path: string) => path === '/settings' || path.startsWith('/settings/') },
]
</script>
<template>
  <header class="mx-auto flex max-w-[1600px] flex-wrap items-center justify-between gap-x-5 gap-y-2 px-4 py-3 sm:px-7 sm:py-4">
    <NuxtLink to="/" aria-label="Forge home" class="flex items-center gap-2.5 text-lg font-bold tracking-tight text-white no-underline">
      <span aria-hidden="true" class="grid size-7 -rotate-6 place-items-center rounded-md border border-mint-200 text-mint-200">F</span>
      Forge
    </NuxtLink>
    <div class="flex flex-wrap items-center gap-x-5 gap-y-2">
      <ActivityPill />
      <nav aria-label="Main navigation" class="flex items-center gap-4 text-sm sm:gap-5">
        <NuxtLink
          v-for="link in links"
          :key="link.to"
          :to="link.to"
          :aria-current="link.current(route.path) ? 'page' : undefined"
          :class="link.current(route.path) ? 'text-mint-200 underline underline-offset-4' : 'text-muted hover:text-white'"
        >{{ link.label }}</NuxtLink>
        <NuxtLink to="/setup-help" aria-label="Help" title="Help" class="inline-flex items-center justify-center text-muted hover:text-white">
          <UIcon name="i-lucide-circle-help" class="size-5" aria-hidden="true" />
        </NuxtLink>
      </nav>
    </div>
  </header>
</template>
