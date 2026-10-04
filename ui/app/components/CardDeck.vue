<script setup lang="ts">
// A small deck of cards, drawn: the header graphic of the "need you" list and
// its empty state. Purely decorative.
const props = withDefaults(defineProps<{ count?: number }>(), { count: 4 })
const deck = computed(() => Math.max(1, Math.min(props.count, 4)))
</script>

<template>
  <span class="card-deck relative block h-11 w-16 shrink-0" aria-hidden="true">
    <span
      v-for="layer in deck"
      :key="layer"
      class="absolute flex h-8 flex-col gap-1 rounded-md border border-white/25 bg-panel px-1.5 pt-1.5 shadow-sm"
      :style="{ top: `${(layer - 1) * 4}px`, left: `${(deck - layer) * 4}px`, right: `${(deck - layer) * 4}px`, opacity: 1 - (deck - layer) * 0.2 }"
    >
      <span v-if="layer === deck" class="block h-1 w-7 rounded-full bg-white/30" />
      <span v-if="layer === deck" class="block h-1 w-10 rounded-full bg-white/15" />
    </span>
  </span>
</template>
