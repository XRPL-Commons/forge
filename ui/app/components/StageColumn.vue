<script setup lang="ts">
import type { CardModel } from '~/utils/factory'

const props = defineProps<{ id: string; title: string; summary: string; cards: CardModel[]; empty?: string }>()
defineEmits<{ stop: [jobId: string] }>()

// Only cards with a worker attached stay expanded; the rest wait as one stack.
const active = computed(() => props.cards.filter(card => card.jobId !== null))
const queued = computed(() => props.cards.filter(card => card.jobId === null))
const open = ref(false)
</script>

<template>
  <div :id="id" class="min-w-0 scroll-mt-32 rounded-xl border border-line bg-bg p-3">
    <div class="flex items-baseline justify-between gap-2">
      <h2 class="text-sm font-semibold text-white">{{ title }}</h2>
    </div>
    <p class="mt-0.5 text-xs text-muted">{{ summary }}</p>
    <TransitionGroup name="card" tag="div" class="relative mt-3 flex flex-col gap-2.5">
      <IssueCard v-for="card in active" :key="card.key" :card="card" @stop="$emit('stop', $event)" />
    </TransitionGroup>
    <div v-if="queued.length" class="mt-3 mb-2">
      <button
        type="button"
        class="issue-stack relative w-full min-w-0 max-w-full overflow-hidden rounded-lg border border-line bg-panel px-4 py-3 text-left transition-colors hover:bg-white/5"
        :aria-expanded="open"
        :aria-controls="id + '-queued'"
        @click="open = !open"
      >
        <span class="pointer-events-none absolute inset-x-2 -bottom-1 h-2 rounded-b-lg border-x border-b border-line bg-panel" aria-hidden="true" />
        <span class="pointer-events-none absolute inset-x-4 -bottom-2 h-2 rounded-b-lg border-x border-b border-line bg-panel" aria-hidden="true" />
        <span class="relative flex items-center justify-between gap-3">
          <span class="text-sm font-semibold text-white">{{ queued.length }} queued</span>
          <span class="text-xs text-muted">{{ open ? 'Collapse' : 'Expand' }}</span>
        </span>
        <span class="relative mt-0.5 block truncate text-xs text-muted">Next: {{ queued[0]!.repo }} · #{{ queued[0]!.number }} — {{ queued[0]!.title }}</span>
      </button>
      <TransitionGroup v-if="open" :id="id + '-queued'" name="card" tag="div" class="relative mt-4 flex flex-col gap-2.5">
        <IssueCard v-for="card in queued" :key="card.key" :card="card" @stop="$emit('stop', $event)" />
      </TransitionGroup>
    </div>
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
