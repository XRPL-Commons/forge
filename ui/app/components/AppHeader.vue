<script setup lang="ts">
const route = useRoute()
const settingsCurrent = computed(() => route.path === '/settings' || route.path.startsWith('/settings/'))
</script>
<template>
  <header class="mx-auto flex max-w-[1600px] flex-wrap items-center justify-between gap-5 px-4 py-3 sm:px-7 sm:py-4">
    <NuxtLink to="/" aria-label="Forge home" class="flex items-center gap-2.5 text-lg font-bold tracking-tight text-white no-underline">
      <span aria-hidden="true" class="grid size-7 -rotate-6 place-items-center rounded-md border border-mint-200 text-mint-200">F</span>
      Forge
    </NuxtLink>
    <div class="flex flex-wrap items-center gap-4 sm:gap-5">
      <ActivityPill />
      <nav aria-label="Main navigation" class="flex items-center gap-4 text-sm sm:gap-5">
        <NuxtLink
          to="/settings"
          :aria-current="settingsCurrent ? 'page' : undefined"
          class="text-muted hover:text-white"
          :class="settingsCurrent ? 'text-mint-200 underline underline-offset-4' : ''"
        >Settings</NuxtLink>
        <NuxtLink to="/setup-help" aria-label="Help" title="Help" class="inline-flex items-center justify-center text-muted hover:text-white">
          <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" aria-hidden="true" focusable="false">
            <circle cx="12" cy="12" r="9" />
            <path d="M9.5 9a2.5 2.5 0 0 1 5 0c0 2-2.5 2-2.5 4m0 3h.01" />
          </svg>
        </NuxtLink>
      </nav>
    </div>
  </header>
</template>
