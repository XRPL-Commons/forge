<template>
  <div class="min-h-screen bg-bg text-white">
    <div class="sticky top-0 z-10 border-b border-line bg-bg">
      <AppHeader />
      <SettingsTabs v-if="showSettingsTabs" />
    </div>
    <InstallationBanner />
    <AccountAlertBar />
    <main class="mx-auto max-w-[1600px] px-4 py-6 sm:px-7">
      <slot />
    </main>
  </div>
</template>
<script setup lang="ts">
const route = useRoute()
const showSettingsTabs = computed(() => route.path.startsWith('/settings'))
</script>
