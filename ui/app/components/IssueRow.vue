<script setup lang="ts">
import { STATUS_NAMES, issuePath, rowFacts, rowPrimary, rowStory, type IssueActionStatus, type IssueRow } from '~/utils/issues'

// One issue on the Issues page, as its own card: a piece of work, with where
// it stands, why, and the one thing to do about it. The whole card opens the
// issue's own route — the title link is stretched over the card, so there is
// exactly one card-sized target and the button beside it stays its own link.
const props = defineProps<{ row: IssueRow; pending?: IssueActionStatus | null; now: number }>()

const to = computed(() => issuePath(props.row.key))
const action = computed(() => rowPrimary(props.row, props.pending))
const story = computed(() => rowStory(props.row))
const facts = computed(() => rowFacts(props.row, props.now))
const tone = computed(() => {
  if (props.row.status === 'attention') return 'text-amber-200'
  if (props.row.status === 'working' || props.row.status === 'completed') return 'text-mint-200'
  if (props.row.status === 'queued') return 'text-sky-200'
  return 'text-muted'
})
</script>

<template>
  <article
    :data-key="row.key"
    :data-status="row.status"
    class="issue-row group relative rounded-xl border border-line bg-panel p-4 transition hover:border-white/30 hover:bg-white/[0.03] sm:p-5"
  >
    <div class="flex items-start justify-between gap-3">
      <p class="flex min-w-0 text-xs text-muted"><span class="truncate">{{ row.repo }}</span><span class="shrink-0 whitespace-pre">{{ ` · #${row.number}` }}</span></p>
      <span
        :class="tone"
        class="inline-flex shrink-0 items-center gap-1.5 rounded-full bg-current/10 px-2 py-0.5 text-xs font-medium ring-1 ring-current/25 ring-inset"
        data-status-chip
      >
        <span class="inline-block size-1.5 rounded-full bg-current" aria-hidden="true" />{{ STATUS_NAMES[row.status] }}
      </span>
    </div>

    <div class="mt-2 flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
      <div class="min-w-0 flex-1">
        <h2 class="line-clamp-2 text-base leading-snug font-semibold text-white">
          <NuxtLink
            :to="to"
            class="issue-link after:absolute after:inset-0 after:rounded-xl after:content-[''] focus-visible:outline-none focus-visible:after:outline-2 focus-visible:after:outline-offset-2 focus-visible:after:outline-sky-200 focus-visible:after:outline-solid group-hover:underline"
          >{{ row.title }}</NuxtLink>
        </h2>
        <p v-if="story.headline || story.plain" class="mt-1.5 line-clamp-2 text-sm" data-story>
          <span class="text-white/85">{{ story.headline }}</span>
          <span v-if="story.plain" class="text-muted">{{ ` ${story.plain}` }}</span>
        </p>
        <ul v-if="facts.length" class="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted" aria-label="Details">
          <li
            v-for="fact in facts"
            :key="fact.key"
            :data-fact="fact.key"
            :class="fact.key.startsWith('pr-') ? 'text-sky-200' : fact.key === 'attempts' || fact.key === 'hold' ? 'text-amber-200' : ''"
          >{{ fact.text }}</li>
        </ul>
      </div>
      <UButton
        :to="to"
        size="sm"
        :color="action.decision ? 'primary' : 'neutral'"
        :variant="action.decision ? 'subtle' : 'outline'"
        class="relative z-10 shrink-0 self-start sm:self-end"
      >{{ action.label }}</UButton>
    </div>
  </article>
</template>
