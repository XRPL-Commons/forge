<script setup lang="ts">
import type { CardFact, IssueCardView } from '~/utils/issueCard'

// One issue as a card, on the Issues page and on Activity alike: the issue's
// cube on the left, then where it lives, its title, one summary and a short
// meta line, and the one thing to do about it on the right.
//
// Two ways in, matching what each page already did:
// - `mode="link"` (Issues): the title is a link stretched over the card, and
//   the action button beside it stays its own link.
// - `mode="open"` (Activity): the whole card is the control. A click, Enter
//   or Space emits `open` (the page opens its panel); a modified or middle
//   click opens the issue's own page in a new tab.
const props = withDefaults(defineProps<{
  view: IssueCardView
  mode?: 'link' | 'open'
  action?: { label: string; to: string; decision: boolean } | null
  compact?: boolean
  heading?: 'h2' | 'h3'
}>(), { mode: 'open', action: null, compact: false, heading: 'h3' })
const emit = defineEmits<{ open: [] }>()

const opens = computed(() => props.mode === 'open')
const label = computed(() => `Open ${props.view.repo}${props.view.number === null ? '' : ` #${props.view.number}`}`)

function activate(event: MouseEvent) {
  if (!opens.value) return
  if (event.button === 1 || event.metaKey || event.ctrlKey || event.shiftKey) window.open(props.view.to, '_blank', 'noopener')
  else if (event.button === 0) emit('open')
}
function key() { if (opens.value) emit('open') }

const TONE: Record<CardFact['tone'], string> = { link: 'text-sky-200', warn: 'text-amber-200', ok: 'text-mint-200', muted: 'text-muted' }
const HEART = 'M6 10.4 1.9 6.4A2.5 2.5 0 0 1 6 3.1a2.5 2.5 0 0 1 4.1 3.3Z'
</script>

<template>
  <article
    :id="view.id"
    :data-key="view.key"
    :role="opens ? 'link' : undefined"
    :tabindex="opens ? 0 : undefined"
    :aria-label="opens ? label : undefined"
    class="issue-card group relative flex min-w-0 gap-3 rounded-xl border border-line bg-panel transition hover:border-white/30 hover:bg-white/[0.03]"
    :class="[
      compact ? 'p-3' : 'p-4 sm:p-5',
      opens ? 'cursor-pointer focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-200' : '',
      view.running ? 'is-working' : '',
    ]"
    :style="{ '--stage-color': `var(--color-stage-${view.look.stage})` }"
    @click="activate"
    @auxclick="activate"
    @mousedown.middle="opens && $event.preventDefault()"
    @keydown.enter.self.prevent="key"
    @keydown.space.self.prevent="key"
  >
    <CubePortrait :look="view.look" :size="compact ? 48 : 60" />

    <div class="min-w-0 flex-1">
      <div class="flex items-start justify-between gap-2">
        <p class="flex min-w-0 text-xs text-muted">
          <span class="truncate">{{ view.repo }}</span><span v-if="view.number !== null" class="shrink-0 whitespace-pre">{{ ` · #${view.number}` }}</span>
        </p>
        <UBadge
          v-if="view.status"
          :color="view.status.color"
          :variant="view.status.dim ? 'outline' : 'subtle'"
          :icon="view.status.icon ?? undefined"
          :size="compact ? 'sm' : 'md'"
          class="shrink-0 rounded-full"
          :class="view.status.dim ? 'text-muted' : ''"
          :data-tone="view.status.dim ? 'calm' : undefined"
          data-status-chip
        >{{ view.status.label }}</UBadge>
      </div>

      <div class="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div class="min-w-0 flex-1">
          <component
            :is="heading"
            class="mt-1 line-clamp-2 font-semibold text-white"
            :class="compact ? 'text-sm leading-5' : 'text-base leading-snug'"
            :title="view.title"
          >
            <NuxtLink
              v-if="!opens"
              :to="view.to"
              class="issue-link after:absolute after:inset-0 after:rounded-xl after:content-[''] focus-visible:outline-none focus-visible:after:outline-2 focus-visible:after:outline-offset-2 focus-visible:after:outline-sky-200 focus-visible:after:outline-solid group-hover:underline"
            >{{ view.title }}</NuxtLink>
            <template v-else>{{ view.title }}</template>
          </component>

          <p v-if="view.headline || view.plain" class="mt-1 line-clamp-2" :class="compact ? 'text-xs' : 'text-sm'" data-story>
            <span class="text-white/85">{{ view.headline }}</span>
            <span v-if="view.plain" class="text-muted">{{ ` ${view.plain}` }}</span>
          </p>

          <ul v-if="view.facts.length" class="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs" :class="compact ? 'mt-1.5' : 'mt-2.5'" aria-label="Details">
            <li v-for="fact in view.facts" :key="fact.key" :data-fact="fact.key" class="inline-flex min-w-0 items-center gap-1" :class="TONE[fact.tone]">
              <span v-if="fact.key === 'progress' && view.running" class="inline-block size-2.5 shrink-0 rounded-full border border-current border-r-transparent motion-safe:animate-spin" aria-hidden="true" />
              <span v-if="fact.hearts" class="inline-flex shrink-0 gap-px" aria-hidden="true" data-hearts>
                <svg v-for="n in fact.hearts.limit" :key="n" viewBox="0 0 12 12" class="size-3" :data-heart="n > fact.hearts.limit - fact.hearts.lost ? 'lost' : 'left'">
                  <path :d="HEART" :class="n > fact.hearts.limit - fact.hearts.lost ? 'fill-white/10 stroke-white/30' : 'fill-rose-300 stroke-rose-300'" stroke-width="1" stroke-linejoin="round" />
                  <path v-if="n > fact.hearts.limit - fact.hearts.lost" d="M6 3.4 5 5.6l1.6.9L5.6 9" class="fill-none stroke-white/40" stroke-width="0.9" />
                </svg>
              </span>
              <NuxtLink v-if="fact.to" :to="fact.to" class="relative z-10 truncate hover:underline" @click.stop @auxclick.stop>{{ fact.text }}</NuxtLink>
              <span v-else class="truncate">{{ fact.text }}</span>
            </li>
          </ul>
        </div>

        <UButton
          v-if="action"
          :to="action.to"
          size="sm"
          :color="action.decision ? 'primary' : 'neutral'"
          :variant="action.decision ? 'subtle' : 'outline'"
          class="relative z-10 shrink-0 self-start sm:self-end"
          @click.stop
        >{{ action.label }}</UButton>
      </div>
    </div>
  </article>
</template>

<style scoped>
.issue-card.is-working { border-color: color-mix(in srgb, var(--stage-color) 55%, transparent); }
</style>
