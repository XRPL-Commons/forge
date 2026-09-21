<script setup lang="ts">
const route = useRoute()
const tabs = [
  { href: '/', label: 'Activity' },
  { href: '/issues', label: 'Issues' },
]
function isCurrent(href: string) {
  if (href === '/') return route.path === '/'
  return route.path === href || route.path.startsWith(href + '/')
}
</script>
<template>
  <nav aria-label="Work navigation" class="mx-auto flex max-w-[1600px] gap-6 px-4 pb-3 text-sm sm:px-7">
    <NuxtLink
      v-for="tab in tabs"
      :key="tab.href"
      :to="tab.href"
      :aria-current="isCurrent(tab.href) ? 'page' : undefined"
      class="pb-1"
      :class="isCurrent(tab.href) ? 'text-mint-200 underline underline-offset-4' : 'text-muted hover:text-white'"
    >{{ tab.label }}</NuxtLink>
  </nav>
</template>
