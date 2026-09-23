<script setup lang="ts">
import type { BreakdownRow } from '~/utils/factory'

// One row per state, the count first, then what the count means right now.
defineProps<{ rows: BreakdownRow[] }>()

const VALUE: Record<BreakdownRow['tone'], string> = { ok: 'text-white', warn: 'text-amber-200', muted: 'text-muted' }
</script>

<template>
  <div class="overflow-hidden rounded-xl border border-line bg-panel">
    <NuxtLink
      v-for="row in rows"
      :key="row.id"
      :to="row.to"
      :data-breakdown="row.id"
      class="grid grid-cols-[3.5rem_9rem_1fr] items-baseline gap-3 border-b border-line px-4 py-2.5 text-sm no-underline last:border-b-0 hover:bg-white/5"
    >
      <span class="text-right text-xl font-semibold tabular-nums" :class="VALUE[row.tone]">{{ row.value }}</span>
      <span class="text-white">{{ row.label }}</span>
      <span class="truncate text-xs text-muted" :title="row.meaning">{{ row.meaning }}</span>
    </NuxtLink>
  </div>
</template>
