<script setup lang="ts">
import type { StatusRow } from '~/utils/status'
import { TONE_TEXT } from '~/utils/tones'

defineProps<{ row: StatusRow }>()
</script>

<template>
  <div :data-status-row="row.id" class="flex flex-col gap-1 py-3 first:pt-0 last:pb-0 sm:flex-row sm:items-center sm:justify-between sm:gap-3">
    <div class="min-w-0">
      <div class="text-xs uppercase tracking-wide text-muted">{{ row.label }}</div>
      <div class="mt-0.5 text-sm" :class="TONE_TEXT[row.tone]">{{ row.value }}</div>
      <div v-if="row.detail" class="mt-0.5 text-xs text-muted">{{ row.detail }}</div>
    </div>
    <NuxtLink v-if="row.to && row.linkLabel" :to="row.to" class="shrink-0 text-xs text-sky-200 hover:underline">{{ row.linkLabel }}</NuxtLink>
  </div>
</template>
