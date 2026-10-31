<script setup lang="ts">
const route = useRoute()
const { poll, pending, headline } = useInstallation()
</script>
<template>
  <aside v-if="pending && route.path !== '/installation'" aria-label="Installation progress" class="mx-auto max-w-[1600px] px-4 pt-4 sm:px-7">
    <div class="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-mint-200/30 bg-panel px-4 py-3">
      <div class="min-w-0">
        <p class="font-medium">Finish setting up Forge</p>
        <p class="mt-1 text-sm text-muted">{{ headline }}</p>
        <p v-if="poll.stale.value" class="mt-1 text-xs text-amber-200">Connection interrupted. Showing the last received setup status.</p>
      </div>
      <UButton to="/installation" color="neutral" variant="outline">View setup progress</UButton>
    </div>
  </aside>
</template>
