<script setup lang="ts">
import type { CardModel } from '~/utils/factory'

defineProps<{ id: string; title: string; summary: string; cards: CardModel[]; empty?: string }>()
defineEmits<{ stop: [jobId: string] }>()
</script>

<template>
  <div :id="id" class="scroll-mt-32 rounded-xl border border-line bg-bg p-3">
    <div class="flex items-baseline justify-between gap-2">
      <h2 class="text-sm font-semibold text-white">{{ title }}</h2>
    </div>
    <p class="mt-0.5 text-xs text-muted">{{ summary }}</p>
    <TransitionGroup name="card" tag="div" class="relative mt-3 flex flex-col gap-2.5">
      <IssueCard v-for="card in cards" :key="card.key" :card="card" @stop="$emit('stop', $event)" />
    </TransitionGroup>
    <p v-if="!cards.length" class="mt-3 text-xs text-muted">{{ empty ?? 'No issues at this stage.' }}</p>
  </div>
</template>

<style scoped>
.card-move, .card-enter-active, .card-leave-active { transition: transform 420ms ease, opacity 420ms ease; }
.card-enter-from, .card-leave-to { opacity: 0; }
.card-leave-active { position: absolute; left: 0; right: 0; }
@media (prefers-reduced-motion: reduce) {
  .card-move, .card-enter-active, .card-leave-active { transition: none; }
}
</style>
