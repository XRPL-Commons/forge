<script setup lang="ts">
const route = useRoute()
const tabs = [
  { href: '/settings', label: 'General', extra: [] as string[] },
  { href: '/settings/repositories', label: 'Repositories', extra: ['/settings/repository'] },
  { href: '/settings/github', label: 'GitHub access', extra: [] as string[] },
  { href: '/settings/agents', label: 'Agent connections', extra: [] as string[] },
  { href: '/settings/harnesses', label: 'Coding accounts', extra: [] as string[] },
]
function isCurrent(tab: (typeof tabs)[number]) {
  return route.path === tab.href || (tab.href === '/settings/repositories' && route.path.startsWith(tab.href + '/')) || tab.extra.includes(route.path)
}
</script>
<template>
  <nav aria-label="Settings navigation" class="mx-auto flex max-w-[1600px] flex-wrap gap-x-6 gap-y-2 px-4 pb-3 text-sm sm:px-7">
    <NuxtLink
      v-for="tab in tabs"
      :key="tab.href"
      :to="tab.href"
      :aria-current="isCurrent(tab) ? 'page' : undefined"
      class="pb-1"
      :class="isCurrent(tab) ? 'text-mint-200 underline underline-offset-4' : 'text-muted hover:text-white'"
    >{{ tab.label }}</NuxtLink>
  </nav>
</template>
