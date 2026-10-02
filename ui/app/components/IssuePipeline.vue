<script setup lang="ts">
import type { PipelineView, StepState } from '~/utils/pipeline'

// "Where this issue is": six small steps, the current one marked, a failure
// or hold shown on the stage where it happened. Words carry the state (the
// note and each step's accessible label); colour only reinforces it.
defineProps<{ view: PipelineView }>()

const DOT: Record<StepState, string> = {
  done: 'bg-mint-500 border-mint-500',
  active: 'bg-sky-200 border-sky-200 ring-4 ring-sky-200/20',
  waiting: 'bg-transparent border-sky-200 ring-4 ring-sky-200/15',
  failed: 'bg-rose-200 border-rose-200 ring-4 ring-rose-200/20',
  blocked: 'bg-amber-200 border-amber-200 ring-4 ring-amber-200/20',
  upcoming: 'bg-transparent border-line',
}
const LABEL: Record<StepState, string> = {
  done: 'text-muted', active: 'text-white font-semibold', waiting: 'text-white font-semibold',
  failed: 'text-rose-200 font-semibold', blocked: 'text-amber-200 font-semibold', upcoming: 'text-muted/70',
}
const SPOKEN: Record<StepState, string> = {
  done: 'done', active: 'in progress', waiting: 'waiting', failed: 'failed', blocked: 'stopped', upcoming: 'not reached',
}
const NOTE: Record<PipelineView['state'], string> = {
  done: 'text-mint-200', active: 'text-sky-200', waiting: 'text-sky-200', failed: 'text-rose-200',
  blocked: 'text-amber-200', upcoming: 'text-muted', closed: 'text-muted',
}
</script>

<template>
  <section id="issue-pipeline" class="mt-3" aria-label="Where this issue is">
    <ol class="grid grid-cols-6">
      <li
        v-for="(step, index) in view.steps"
        :key="step.id"
        :data-stage="step.id"
        :data-state="step.state"
        :aria-current="step.id === view.current ? 'step' : undefined"
        :aria-label="`${step.label}: ${SPOKEN[step.state]}`"
        class="relative flex min-w-0 flex-col items-center gap-1.5 text-center"
      >
        <!-- The connector runs from this dot to the previous one. -->
        <span
          v-if="index > 0"
          aria-hidden="true"
          class="absolute right-1/2 top-[5px] h-0.5 w-full"
          :class="step.state === 'upcoming' ? 'bg-line' : 'bg-mint-700'"
        />
        <span aria-hidden="true" class="relative z-[1] size-3 rounded-full border-2" :class="DOT[step.state]" />
        <span class="w-full break-words px-0.5 text-[11px] leading-tight sm:text-xs" :class="LABEL[step.state]">
          <span class="sm:hidden">{{ step.short }}</span>
          <span class="hidden sm:inline">{{ step.label }}</span>
        </span>
      </li>
    </ol>
    <p class="mt-2 text-xs" :class="NOTE[view.state]">
      <span id="issue-stage-note">{{ view.note }}</span>
      <template v-if="view.attempts">
        <span class="text-muted" aria-hidden="true"> · </span>
        <span id="issue-attempts" class="text-muted">{{ view.attempts }}</span>
      </template>
    </p>
  </section>
</template>
