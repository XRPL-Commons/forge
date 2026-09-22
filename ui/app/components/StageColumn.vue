<script setup lang="ts">
import type { CardModel } from '~/utils/factory'

const props = defineProps<{ id: string; title: string; summary: string; cards: CardModel[]; empty?: string }>()
defineEmits<{ stop: [jobId: string] }>()

// Only cards with a worker attached stay expanded; the rest wait as one stack.
const active = computed(() => props.cards.filter(card => card.jobId !== null))
const queued = computed(() => props.cards.filter(card => card.jobId === null))
const open = ref(false)
const role = computed(() => props.id.replace(/^stage-/, '') as 'build' | 'review' | 'audit' | 'recovery')
const deck = computed(() => Math.min(queued.value.length, 4))
</script>

<template>
  <div :id="id" class="min-w-0 scroll-mt-32 rounded-xl border border-line bg-bg p-3">
    <div class="flex items-baseline justify-between gap-2">
      <h2 class="text-sm font-semibold text-white">{{ title }}</h2>
    </div>
    <p class="mt-0.5 text-xs text-muted">{{ summary }}</p>
    <TransitionGroup name="card" tag="div" class="relative mt-3 flex flex-col gap-2.5">
      <IssueCard v-for="card in active" :key="card.key" :card="card" :role="role" @stop="$emit('stop', $event)" />
    </TransitionGroup>
    <div v-if="queued.length" class="mt-3 flex flex-col items-center">
      <!-- Queued issues wait as a small deck; the count sits under it and the
           next title is the tooltip. Opening the deck lists the cards. -->
      <button
        type="button"
        class="issue-stack group flex flex-col items-center gap-1.5 rounded-lg px-4 py-2 text-center transition-colors hover:bg-white/5"
        :aria-expanded="open"
        :aria-controls="id + '-queued'"
        :title="`Next: ${queued[0]!.repo} · #${queued[0]!.number} — ${queued[0]!.title}`"
        @click="open = !open"
      >
        <span class="relative block h-11 w-16" aria-hidden="true">
          <span
            v-for="layer in deck"
            :key="layer"
            class="absolute flex h-8 flex-col gap-1 rounded-md border border-white/25 bg-panel px-1.5 pt-1.5 shadow-sm transition-transform group-hover:-translate-y-0.5"
            :style="{ top: `${(layer - 1) * 4}px`, left: `${(deck - layer) * 4}px`, right: `${(deck - layer) * 4}px`, opacity: 1 - (deck - layer) * 0.2 }"
          >
            <span v-if="layer === deck" class="block h-1 w-7 rounded-full bg-white/30" />
            <span v-if="layer === deck" class="block h-1 w-10 rounded-full bg-white/15" />
          </span>
        </span>
        <span class="text-xs font-semibold text-white">{{ queued.length }} queued</span>
        <span class="text-[11px] text-muted">{{ open ? 'Collapse' : 'Expand' }}</span>
      </button>
      <TransitionGroup v-if="open" :id="id + '-queued'" name="card" tag="div" class="relative mt-3 flex w-full flex-col gap-2.5">
        <IssueCard v-for="card in queued" :key="card.key" :card="card" :role="role" @stop="$emit('stop', $event)" />
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
