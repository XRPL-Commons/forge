<script setup lang="ts">
import type { WorkbenchLook } from '~/utils/workbench'

// Under the workbench: the stages this issue's cube has earned (the role the
// pipeline stepper used to play) and the attempts it has left.
defineProps<{ look: WorkbenchLook }>()

const SPOKEN = { done: 'passed', current: 'current stage', future: 'not reached' } as const
</script>

<template>
  <div id="issue-stats" class="flex flex-wrap items-end justify-between gap-x-5 gap-y-3 rounded-xl border border-line bg-panel px-3 py-2.5">
    <div>
      <span class="mb-1 block text-[11px] uppercase tracking-wider text-muted">Stages earned</span>
      <ol id="issue-stages" class="flex gap-1.5" aria-label="Stages earned">
        <li
          v-for="badge in look.badges"
          :key="badge.id"
          :data-stage="badge.id"
          :data-state="badge.state"
          :aria-current="badge.state === 'current' ? 'step' : undefined"
          :aria-label="`${badge.label}: ${SPOKEN[badge.state]}`"
          class="grid w-11 justify-items-center gap-0.5 text-[10px] leading-tight"
          :class="badge.state === 'future' ? 'text-muted/60' : badge.state === 'current' ? 'font-semibold text-white' : 'text-muted'"
        >
          <svg width="22" height="22" viewBox="0 0 22 22" aria-hidden="true">
            <rect
              x="2" y="2" width="18" height="18" rx="5"
              :fill="badge.state === 'future' ? 'none' : `var(--color-stage-${badge.id})`"
              :fill-opacity="badge.state === 'current' ? 0.45 : 1"
              :stroke="badge.state === 'future' ? 'var(--color-line)' : '#0a0c0f'"
              stroke-width="1.6"
              :stroke-dasharray="badge.state === 'future' ? '3 2.5' : undefined"
            />
            <template v-if="badge.state !== 'future'">
              <circle cx="8.5" cy="11" r="1.4" fill="#12151a" />
              <circle cx="13.5" cy="11" r="1.4" fill="#12151a" />
            </template>
          </svg>
          {{ badge.label }}
        </li>
      </ol>
    </div>
    <div v-if="look.attempts">
      <span class="mb-1 block text-[11px] uppercase tracking-wider text-muted">Attempts</span>
      <div id="issue-lives" class="flex items-center gap-1 text-xs text-muted">
        <svg
          v-for="index in look.attempts.limit"
          :key="index"
          width="18" height="16" viewBox="0 0 18 16" aria-hidden="true"
          :data-lost="index > look.attempts.left ? 'true' : undefined"
        >
          <path
            d="M9 14.5s-6.5-4-6.5-8.5A3.5 3.5 0 0 1 9 4a3.5 3.5 0 0 1 6.5 2c0 4.5-6.5 8.5-6.5 8.5z"
            :fill="index <= look.attempts.left ? '#edf1ee' : 'none'"
            :stroke="index <= look.attempts.left ? '#0a0c0f' : 'var(--color-muted)'"
            stroke-width="1.5"
            :stroke-dasharray="index <= look.attempts.left ? undefined : '2 2'"
          />
        </svg>
        <span id="issue-lives-text" class="ml-1.5">{{ look.attempts.text }}</span>
      </div>
    </div>
  </div>
</template>
