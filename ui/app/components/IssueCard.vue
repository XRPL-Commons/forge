<script setup lang="ts">
import { elapsed, type CardModel } from '~/utils/factory'

const props = defineProps<{ card: CardModel }>()
defineEmits<{ stop: [jobId: string] }>()

// Every card renders the same fixed set of rows — never a conditional tree —
// so all cards are the same height whatever they carry. The header and the
// footer never shrink; only the middle block gives way on a narrow screen.
const timer = computed(() => {
  const { timerStart, timerEnd } = props.card
  if (timerStart === null || timerEnd === null) return ''
  return elapsed(timerEnd - timerStart)
})
const progress = computed(() => [props.card.providerLine, timer.value].filter(Boolean).join(' · '))
</script>

<template>
  <article
    :id="card.id"
    :data-key="card.key"
    class="issue-card flex h-[136px] flex-col overflow-hidden rounded-xl border border-line bg-panel px-3 py-2.5 sm:h-[168px] sm:px-4 sm:py-3"
    :class="card.running ? 'border-mint-200/40' : ''"
  >
    <div class="shrink-0">
      <div class="flex items-baseline justify-between gap-2 text-xs text-muted">
        <span class="flex min-w-0 items-baseline gap-1">
          <span class="truncate">{{ card.repo }}</span>
          <span v-if="card.number !== null" class="shrink-0">· #{{ card.number }}</span>
        </span>
        <span class="shrink-0" :class="card.running ? 'text-mint-200' : ''">{{ card.phaseLabel }}</span>
      </div>
      <h3 class="mt-1 h-10 overflow-hidden text-sm font-semibold text-white" :title="card.title">
        <span class="line-clamp-2">{{ card.title }}</span>
      </h3>
    </div>

    <div class="min-h-0 flex-1 overflow-hidden pt-1 text-xs text-muted">
      <p class="truncate">
        <span v-if="card.running" class="mr-1.5 inline-block size-2.5 align-[-1px] rounded-full border border-current border-r-transparent text-mint-200 motion-safe:animate-spin" aria-hidden="true" />{{ progress }}
      </p>
      <p class="truncate" :title="card.reason">{{ card.reason }}</p>
    </div>

    <div class="flex shrink-0 items-center gap-2">
      <UButton :to="card.to" size="xs" color="neutral" variant="outline">Open</UButton>
      <slot name="actions">
        <UButton v-if="card.stoppable && card.jobId" size="xs" color="error" variant="ghost" @click="$emit('stop', card.jobId)">Stop</UButton>
      </slot>
    </div>

    <div class="mt-1 h-4 shrink-0 truncate text-xs">
      <NuxtLink v-if="card.footerLink && card.footerLink.to" :to="card.footerLink.to" class="text-sky-200 hover:underline">{{ card.footerLink.label }}</NuxtLink>
      <span v-else-if="card.footerLink" class="text-amber-200">{{ card.footerLink.label }}</span>
    </div>
  </article>
</template>
