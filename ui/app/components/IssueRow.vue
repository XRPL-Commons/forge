<script setup lang="ts">
import { STATUS_NAMES, issuePath, rowAction, type IssueActionStatus, type IssueRow } from '~/utils/issues'

// One row of the issues list. Every row is a link to the issue's own route —
// there is no second detail view — and the pill on the right says what
// opening it is for, or that a decision is still being applied.
const props = defineProps<{ row: IssueRow; pending?: IssueActionStatus | null }>()

const to = computed(() => issuePath(props.row.key))
const action = computed(() => rowAction(props.row, props.pending))
const tone = computed(() => {
  if (props.row.status === 'attention') return 'text-amber-200'
  if (props.row.status === 'working' || props.row.status === 'completed') return 'text-mint-200'
  if (props.row.status === 'queued') return 'text-sky-200'
  return 'text-muted'
})
</script>

<template>
  <article :data-key="row.key" class="issue-row flex flex-wrap items-start gap-3 border-b border-line px-1 py-3 last:border-b-0 sm:flex-nowrap">
    <div class="min-w-0 flex-1">
      <p class="text-xs text-muted">{{ row.repo }} · #{{ row.number }}</p>
      <h2 class="mt-0.5 text-sm font-semibold">
        <NuxtLink :to="to" class="text-white hover:underline">{{ row.title }}</NuxtLink>
      </h2>
      <p class="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
        <span :class="tone" class="flex items-center gap-1.5">
          <span class="inline-block size-2 rounded-full bg-current" aria-hidden="true" />{{ STATUS_NAMES[row.status] }}
        </span>
        <span class="text-muted">{{ row.explanation }}</span>
      </p>
      <p v-if="row.pull_requests.length" class="mt-1 text-xs text-sky-200">Pull request #{{ row.pull_requests[0]!.number }}</p>
    </div>
    <UButton :to="to" size="xs" color="neutral" variant="outline" class="shrink-0">{{ action }}</UButton>
  </article>
</template>
