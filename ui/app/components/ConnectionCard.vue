<script setup lang="ts">
import type { ConnectionCardModel } from '~/utils/repositories'

// Every sentence comes from `connectionCards()`; the card only lays them out,
// so GitHub access and repository setup cannot drift apart.
defineProps<{ card: ConnectionCardModel }>()
</script>

<template>
  <article :data-key="card.role" class="rounded-xl border border-line bg-panel p-4">
    <h3 class="text-sm font-semibold text-white">
      {{ card.label }} <span class="text-muted">–</span> <span class="break-all font-medium text-muted">{{ card.identity }}</span>
    </h3>
    <p class="mt-1 text-xs text-muted">{{ card.purpose }}</p>
    <div class="mt-3 flex flex-wrap items-center gap-x-3 gap-y-2 text-xs">
      <span data-badge><UBadge :color="card.ready ? 'primary' : 'warning'" variant="subtle" size="sm">{{ card.status }}</UBadge></span>
      <p class="text-muted">{{ card.observation }}</p>
      <NuxtLink
        v-if="card.action"
        class="ms-auto text-sky-200 underline-offset-4 hover:underline"
        :to="card.action.href"
        :target="card.action.external ? '_blank' : undefined"
        :rel="card.action.external ? 'noopener noreferrer' : undefined"
        :external="card.action.external"
      >{{ card.action.label }}</NuxtLink>
    </div>
    <p v-for="error in card.errors" :key="error" class="mt-2 text-xs text-rose-200">{{ error }}</p>
    <p v-if="card.recommendation" data-recommendation class="mt-2 text-xs text-muted">{{ card.recommendation }}</p>
  </article>
</template>
