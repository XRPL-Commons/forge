<script setup lang="ts">
import type { CardModel, FactoryView, IssueGroups } from '~/utils/factory'
import { LINE_REASON_TEXT, lineState } from '~/utils/factoryLine'
import { modelCardView } from '~/utils/issueCard'
import {
  NEEDS_FILTERS, NEEDS_FILTER_LABELS, needsAction, needsCounts, needsFiltered, needsYouRows, unblocksLabel,
  type NeedsFilter,
} from '~/utils/needsYou'
import { panelQuery, panelTarget } from '~/utils/panel'

// The issues in the factory line's crate, as cards: most unblocking first,
// filtered to one bay by the chips. A card replaces the list: the modal
// closes and the page opens that issue's panel.
const props = defineProps<{ view: FactoryView | null; now: number; groups?: IssueGroups }>()
const open = defineModel<boolean>('open', { default: false })
const filter = defineModel<NeedsFilter>('filter', { default: 'all' })
const emit = defineEmits<{ open: [card: CardModel] }>()

const route = useRoute()
const router = useRouter()

const rows = computed(() => needsYouRows(lineState(props.view, props.now, props.groups), props.view))
const counts = computed(() => needsCounts(rows.value))
const shown = computed(() => needsFiltered(rows.value, filter.value))
// Only the bays that hold issues get a chip (and the one already chosen).
const options = computed(() => NEEDS_FILTERS.filter(option => option === 'all' || counts.value[option] || option === filter.value))
const expanded = ref<string | null>(null)
const stewardDecisions = computed(() => new Map((props.view?.issues ?? []).filter(issue => issue.steward_decision)
  .map(issue => [issue.key, issue.steward_decision!])))
watch(open, value => { if (!value) expanded.value = null })
const title = computed(() => rows.value.length ? `${rows.value.length} need${rows.value.length === 1 ? 's' : ''} you` : 'Nothing needs you')

function actionFor(card: CardModel, reason: Parameters<typeof needsAction>[0]) {
  return { ...needsAction(reason), to: router.resolve({ path: route.path, query: panelQuery(route.query, panelTarget(card)) }).href }
}
function choose(card: CardModel) {
  open.value = false
  emit('open', card)
}
</script>

<template>
  <UModal
    v-model:open="open"
    :title="title"
    description="Most unblocking first: closing these lets the most other issues move."
    :ui="{ content: 'max-w-[min(56rem,calc(100vw-2rem))]', header: 'hidden', body: 'p-4 sm:p-5' }"
  >
    <template #body>
      <div class="needs-you" data-needs-you>
        <div class="flex items-start justify-between gap-3">
          <div class="flex items-center gap-3">
            <CardDeck :count="rows.length" />
            <div>
              <h2 class="text-base font-semibold text-white" data-needs-title>{{ title }}</h2>
              <p class="text-xs text-muted">Most unblocking first.</p>
            </div>
          </div>
          <UButton icon="i-lucide-x" color="neutral" variant="ghost" size="sm" aria-label="Close" @click="open = false" />
        </div>

        <div v-if="rows.length" class="mt-4 flex flex-wrap gap-1.5" role="group" aria-label="Filter by reason">
          <UButton
            v-for="option in options"
            :key="option"
            size="xs"
            :color="filter === option ? 'primary' : 'neutral'"
            :variant="filter === option ? 'subtle' : 'outline'"
            class="rounded-full"
            :aria-pressed="filter === option"
            :data-needs-filter="option"
            :title="option === 'all' ? undefined : LINE_REASON_TEXT[option]"
            @click="filter = option"
          >
            <BayGlyph v-if="option !== 'all'" :reason="option" :size="16" />
            {{ NEEDS_FILTER_LABELS[option] }} <span class="tabular-nums text-muted">{{ counts[option] }}</span>
          </UButton>
        </div>
        <p v-if="rows.length && filter !== 'all'" class="mt-2 text-xs text-white/85" data-needs-reason>{{ LINE_REASON_TEXT[filter] }}</p>

        <div v-if="shown.length" class="mt-3 grid gap-2.5 md:grid-cols-2" data-needs-list>
          <div v-for="row in shown" :key="row.item.id" class="min-w-0 space-y-2">
            <IssueCard
              :view="modelCardView(row.item.card, now)"
              :action="actionFor(row.item.card, row.reason)"
              :tag="unblocksLabel(row.unblocks)"
              compact
              @open="choose(row.item.card)"
            />
            <template v-if="stewardDecisions.has(row.item.id)">
              <UButton size="xs" color="primary" variant="outline" :aria-expanded="expanded === row.item.id"
                @click="expanded = expanded === row.item.id ? null : row.item.id">
                {{ expanded === row.item.id ? 'Hide choices' : 'Review steward choices' }}
              </UButton>
              <LazyStewardDecision v-if="open && expanded === row.item.id" :issue-key="row.item.id"
                :expected="stewardDecisions.get(row.item.id)" />
            </template>
          </div>
        </div>
        <div v-else class="mt-6 flex flex-col items-center gap-2 pb-4 text-center" data-needs-empty>
          <CardDeck :count="1" class="opacity-60" />
          <p class="text-sm text-white">{{ rows.length ? 'Nothing in this group right now' : 'Nothing needs you right now' }}</p>
          <p class="text-xs text-muted">{{ rows.length ? 'Choose All to see the rest.' : 'Forge will light up the crate when it does.' }}</p>
        </div>
      </div>
    </template>
  </UModal>
</template>
