<template>
  <div class="min-h-screen bg-bg text-white">
    <div class="sticky top-0 z-10 border-b border-line bg-bg">
      <AppHeader />
      <WorkTabs v-if="showWorkTabs" />
      <SettingsTabs v-else-if="showSettingsTabs" />
    </div>
    <main class="mx-auto max-w-[1600px] px-4 py-6 sm:px-7">
      <slot />
    </main>
  </div>
</template>
<script setup lang="ts">
const route = useRoute()
const showWorkTabs = computed(() => ['/', '/issues'].includes(route.path) || route.path.startsWith('/issues/'))
const showSettingsTabs = computed(() => route.path.startsWith('/settings'))
</script>
