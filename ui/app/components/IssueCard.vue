<script setup lang="ts">
import { elapsed, type CardModel } from '~/utils/factory'

const props = defineProps<{ card: CardModel; role?: 'build' | 'review' | 'audit' | 'recovery' | 'merge' }>()
const ROLE_COLORS = { build: '#94bfff', review: '#c9a4f4', audit: '#80d7bd', recovery: '#c0cbcd', merge: '#edbd77' } as const
const roleColor = computed(() => ROLE_COLORS[props.role ?? 'build'])
defineEmits<{ stop: [jobId: string] }>()

// Every card renders the same fixed set of rows — never a conditional tree —
// so all cards are the same height whatever they carry. The header and the
// footer never shrink; only the middle block gives way on a narrow screen.
const timer = computed(() => {
  const { timerStart, timerEnd } = props.card
  if (timerStart === null || timerEnd === null) return ''
  return elapsed(timerEnd - timerStart)
})
// One summary line carries provider, elapsed time and the note (attempts,
// reason); the note is linked when the model gives it a target.
const progress = computed(() => [props.card.providerLine, timer.value].filter(Boolean).join(' · '))
// A completed card's most useful line is where the merge got to, so the
// deployment verdict takes the note over the outcome it already reported.
const note = computed(() => props.card.footerLink?.label || props.card.deployment || props.card.reason || '')
</script>

<template>
  <article
    :id="card.id"
    :data-key="card.key"
    class="issue-card relative flex h-[104px] flex-col overflow-hidden rounded-xl border border-line bg-panel px-3 py-2"
    :class="card.running ? 'is-working' : ''"
    :style="{ '--role-color': roleColor }"
  >
    <span class="work-wave top" aria-hidden="true" /><span class="work-wave bottom" aria-hidden="true" />
    <div class="flex items-baseline justify-between gap-2 text-xs text-muted">
      <span class="flex min-w-0 items-baseline gap-1">
        <span class="truncate">{{ card.repo }}</span>
        <span v-if="card.number !== null" class="shrink-0">· #{{ card.number }}</span>
      </span>
      <span class="shrink-0" :class="card.running ? 'text-mint-200' : ''">{{ card.phaseLabel }}</span>
    </div>
    <h3 class="mt-0.5 h-10 overflow-hidden text-sm font-semibold leading-5 text-white" :title="card.title">
      <span class="line-clamp-2">{{ card.title }}</span>
    </h3>

    <div class="mt-auto flex items-center justify-between gap-2 text-xs text-muted">
      <p class="flex min-w-0 items-center gap-1.5" :title="[progress, note].filter(Boolean).join(' · ')">
        <span v-if="card.running" class="inline-block size-2.5 shrink-0 rounded-full border border-current border-r-transparent text-mint-200 motion-safe:animate-spin" aria-hidden="true" />
        <span v-if="progress" class="truncate">{{ progress }}</span>
        <span v-if="progress && note" class="shrink-0">·</span>
        <NuxtLink v-if="card.footerLink && card.footerLink.to" :to="card.footerLink.to" class="truncate text-sky-200 hover:underline">{{ card.footerLink.label }}</NuxtLink>
        <span v-else-if="note" class="truncate" :class="card.footerLink ? 'text-amber-200' : ''">{{ note }}</span>
      </p>
      <span class="-mr-1.5 flex shrink-0 items-center">
        <UButton :to="card.to" size="xs" color="neutral" variant="ghost" icon="i-lucide-arrow-up-right" aria-label="Open" title="Open" />
        <slot name="actions">
          <UButton v-if="card.stoppable && card.jobId" size="xs" color="error" variant="ghost" icon="i-lucide-square" aria-label="Stop" title="Stop" @click="$emit('stop', card.jobId)" />
        </slot>
      </span>
    </div>
  </article>
</template>

<style scoped>
/* A working card carries a light wave along its top and bottom edges, in its role's colour. */
.issue-card.is-working { border-color: var(--role-color, #94bfff); }
.work-wave { position: absolute; left: 0; width: 100%; height: 3px; pointer-events: none; opacity: 0; overflow: hidden; }
.work-wave.top { top: 0; }
.work-wave.bottom { bottom: 0; }
.work-wave::after { content: ""; position: absolute; height: 100%; width: 55%; left: -55%; background: linear-gradient(90deg, transparent, var(--role-color, #94bfff), transparent); animation: work-wave 3s linear infinite; animation-play-state: paused; }
.work-wave.bottom::after { animation-delay: -1.5s; animation-direction: reverse; }
.is-working .work-wave { opacity: 1; }
.is-working .work-wave::after { animation-play-state: running; }
@keyframes work-wave { to { left: 100%; } }
@media (prefers-reduced-motion: reduce) { .work-wave::after { animation: none; left: 22%; } }
</style>
