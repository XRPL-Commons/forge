<script setup lang="ts">
// The global coding-account alert: under the top navigation on every page,
// and at the top of the issue drawer, whenever an account cannot work. It
// says what is wrong and links to the exact place it is fixed. Dismissing
// hides it for this session until something changes.
defineProps<{ inset?: boolean }>()
const { poll, alerts, visible, dismiss } = useCodingAccounts()
onMounted(poll.start)
</script>

<template>
  <div
    v-if="visible && alerts.length"
    :class="inset ? 'mb-4 space-y-2' : 'mx-auto max-w-[1600px] space-y-2 px-4 pt-4 sm:px-7'"
    data-account-alerts
  >
    <UAlert
      v-for="alert in alerts"
      :key="alert.provider"
      :color="alert.color"
      variant="subtle"
      :icon="alert.status === 'sign_in_expired' ? 'i-lucide-key-round' : 'i-lucide-triangle-alert'"
      :title="alert.title"
      :description="alert.description"
      role="alert"
      :data-account-alert="alert.provider"
      :data-status="alert.status"
      :orientation="inset ? 'vertical' : 'horizontal'"
      :actions="[{ label: alert.actionLabel, to: alert.to, color: alert.color, variant: 'solid', size: 'sm' }]"
      :close="{ color: 'neutral', variant: 'link' }"
      @update:open="(value: boolean) => { if (!value) dismiss() }"
    />
  </div>
</template>
