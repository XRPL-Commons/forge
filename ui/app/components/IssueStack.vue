<script setup lang="ts">
import type { CardModel } from '~/utils/factory'

// One deck of cards the operator does not have to read right now: queued work
// in a stage column, or issues deliberately on hold. The deck keeps the board
// compact — clicking it opens the cards in a modal instead of pushing the
// sections below it down the page.
const props = defineProps<{
  cards: CardModel[]
  // What the cards are, read after the count: "11 queued", "3 on hold".
  noun: string
  // The board section the deck belongs to, appended to the modal title.
  section?: string
  role?: 'build' | 'review' | 'audit' | 'recovery' | 'merge'
}>()
defineEmits<{ stop: [jobId: string] }>()

const open = ref(false)
const deck = computed(() => Math.min(props.cards.length, 4))
const label = computed(() => `${props.cards.length} ${props.noun}`)
const title = computed(() => (props.section ? `${label.value} · ${props.section}` : label.value))
const next = computed(() => props.cards[0] ?? null)
</script>

<template>
  <div class="flex w-full flex-col items-center">
    <!-- The count sits under the deck and the next title is the tooltip. -->
    <button
      type="button"
      class="issue-stack group flex flex-col items-center gap-1.5 rounded-lg px-4 py-2 text-center transition-colors hover:bg-white/5"
      :aria-expanded="open"
      aria-haspopup="dialog"
      :title="next ? `Next: ${next.repo}${next.number === null ? '' : ` · #${next.number}`} — ${next.title}` : undefined"
      @click="open = true"
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
      <span class="text-xs font-semibold text-white">{{ label }}</span>
      <span class="text-[11px] text-muted">Open</span>
    </button>

    <UModal v-model:open="open" :title="title" :ui="{ content: 'max-w-[min(64rem,92vw)]' }">
      <template #body>
        <div class="grid gap-2.5 sm:grid-cols-2 xl:grid-cols-3">
          <IssueCard v-for="card in cards" :key="card.key" :card="card" :role="role" @stop="$emit('stop', $event)" />
        </div>
      </template>
    </UModal>
  </div>
</template>
