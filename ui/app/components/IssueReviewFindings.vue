<script setup lang="ts">
import { reviewRows, summaryBlock, type IssueDetail } from '~/utils/issues'
const props = defineProps<{ detail: IssueDetail }>()
const detailsOpen = useState(`issue-details-${props.detail.key}`, () => false)
const summary = computed(() => summaryBlock(props.detail))
const reviews = computed(() => reviewRows(props.detail.reviews))
const expanded = ref(new Set<string>())
function toggleReview(role: string) {
  const next = new Set(expanded.value)
  if (!next.delete(role)) next.add(role)
  expanded.value = next
}
</script>
<template>
            <IssueSection id="issue-details" v-model:open="detailsOpen" title="Review findings">
              <p v-if="summary?.details" id="issue-explanation" class="whitespace-pre-line text-sm text-muted wrap-anywhere">{{ summary.details }}</p>
              <div v-if="reviews.length" id="issue-reviews" class="mt-3 flex flex-col gap-2">
                <div v-for="row in reviews" :key="row.role" class="rounded-xl border border-line bg-panel px-3 py-2">
                  <div class="flex flex-wrap items-center gap-2">
                    <UBadge :color="row.tone" variant="subtle" size="sm">{{ row.outcomeLabel }}</UBadge>
                    <span class="text-sm font-semibold text-white">{{ row.label }}</span>
                    <UButton
                      v-if="row.more"
                      size="xs"
                      color="neutral"
                      variant="ghost"
                      class="ml-auto"
                      @click="toggleReview(row.role)"
                    >{{ expanded.has(row.role) ? 'Less' : 'More' }}</UButton>
                  </div>
                  <p class="mt-1 text-sm text-muted wrap-anywhere" :class="expanded.has(row.role) ? '' : 'line-clamp-2'">
                    {{ expanded.has(row.role) ? row.summary : row.lead }}
                  </p>
                </div>
              </div>
            </IssueSection>

</template>
