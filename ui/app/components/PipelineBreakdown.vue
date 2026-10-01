<script setup lang="ts">
import type { BreakdownRow } from '~/utils/factory'
import { TONE_TEXT } from '~/utils/tones'

// One stat box per state: the label, the count, and a fixed phrase that never
// changes shape from one poll to the next. What the count means right now is
// derived copy, so it is the box's tooltip and its described-by text rather
// than a line that reflows the grid.
defineProps<{ rows: BreakdownRow[] }>()
</script>

<template>
  <div class="grid grid-cols-2 gap-2.5 sm:grid-cols-3 lg:grid-cols-5">
    <NuxtLink
      v-for="row in rows"
      :key="row.id"
      :to="row.to"
      :data-breakdown="row.id"
      :title="row.meaning"
      :aria-describedby="`breakdown-${row.id}-meaning`"
      class="flex min-w-0 flex-col rounded-xl border border-line bg-panel px-4 py-3 no-underline transition-colors hover:bg-white/5"
    >
      <span class="truncate text-xs text-muted">{{ row.label }}</span>
      <span class="mt-1 text-2xl font-semibold leading-8 tabular-nums" :class="TONE_TEXT[row.tone]">{{ row.value }}</span>
      <span class="truncate text-xs text-muted">{{ row.detail }}</span>
      <span :id="`breakdown-${row.id}-meaning`" class="sr-only">{{ row.meaning }}</span>
    </NuxtLink>
  </div>
</template>
