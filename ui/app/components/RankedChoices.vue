<script setup lang="ts">
import { rankedDetails, rankedChoices, choiceLabel, splitChildren, type RankedDecision, type StewardSelection, type StewardRepository, type StewardScope } from '~/utils/steward'

const props = withDefaults(defineProps<{
  decision: RankedDecision
  repositories?: StewardRepository[]
  scope?: StewardScope | null
  disabled?: boolean
  pendingChoice?: string | null
  selectedChoice?: string | null
}>(), { repositories: () => [], disabled: false, pendingChoice: null, selectedChoice: null })
const emit = defineEmits<{ select: [selection: StewardSelection] }>()
const choices = computed(() => rankedChoices(props.decision))
function choose(choiceId: string) {
  if (props.disabled || props.pendingChoice) return
  emit('select', { decision_id: props.decision.decision_id, choice_id: choiceId, digest: props.decision.digest })
}
</script>

<template>
  <section aria-label="Steward choices" class="space-y-3" data-steward-choices>
    <div class="flex flex-wrap items-baseline justify-between gap-2">
      <h3 class="text-sm font-semibold text-white">Choose the next step</h3>
      <p class="text-xs text-muted">Steward confidence: {{ Math.round(decision.confidence * 100) }}%</p>
    </div>
    <div v-if="scope?.issues.length" class="rounded-lg border border-line bg-panel p-3 text-sm" aria-label="Issues affected by this choice">
      <p class="font-medium text-white">This choice applies to all {{ scope.issues.length }} {{ scope.issues.length === 1 ? 'issue' : 'issues' }} in this delivery.</p>
      <ul class="mt-1 space-y-1 text-muted">
        <li v-for="issue in scope.issues" :key="issue.key">#{{ issue.number }} — {{ issue.title }}</li>
      </ul>
    </div>
    <ol class="space-y-2.5" aria-label="Ranked choices">
      <li v-for="(choice, index) in choices" :key="choice.id" class="rounded-xl border p-3 sm:p-4"
        :class="choice.id === decision.recommended_choice_id ? 'border-primary/45 bg-primary/5' : 'border-line bg-panel'"
        :data-choice-id="choice.id">
        <div class="flex flex-wrap items-center gap-2">
          <span class="text-xs tabular-nums text-muted" aria-hidden="true">{{ index + 1 }}.</span>
          <h4 class="text-sm font-semibold text-white">{{ choiceLabel(choice.action) }}</h4>
          <UBadge v-if="choice.id === selectedChoice" size="sm" color="info" variant="subtle">Chosen</UBadge>
          <UBadge v-if="choice.id === decision.recommended_choice_id" size="sm" color="primary" variant="subtle">Recommended</UBadge>
        </div>
        <p class="mt-2 whitespace-pre-line text-sm text-white/85 wrap-anywhere">{{ choice.reason }}</p>
        <dl v-if="rankedDetails(choice, repositories).length" class="mt-2 space-y-1 text-xs">
          <div v-for="detail in rankedDetails(choice, repositories)" :key="detail.label">
            <dt class="inline text-muted">{{ detail.label }}: </dt>
            <dd class="inline whitespace-pre-line text-white/85 wrap-anywhere">{{ detail.value }}</dd>
          </div>
        </dl>
        <details v-if="choice.action === 'split'" class="mt-2 text-sm">
          <summary class="cursor-pointer text-primary">Review {{ splitChildren(choice).length }} proposed issues</summary>
          <ol class="mt-2 space-y-3 pl-3">
            <li v-for="(child, childIndex) in splitChildren(choice)" :key="childIndex" class="border-l border-line pl-3">
              <p class="font-semibold text-white">{{ child.title }}</p>
              <p class="mt-1 whitespace-pre-line text-xs text-muted wrap-anywhere">{{ child.body }}</p>
              <ul class="mt-1 list-disc pl-4 text-xs text-muted" aria-label="Acceptance criteria">
                <li v-for="(criterion, criterionIndex) in child.acceptance_criteria" :key="criterionIndex">{{ criterion }}</li>
              </ul>
            </li>
          </ol>
        </details>
        <details v-if="decision.brief?.choice_id === choice.id && (!selectedChoice || selectedChoice === choice.id)"
          class="mt-2 text-xs text-muted">
          <summary class="cursor-pointer">Worker instructions for this choice</summary>
          <p class="mt-2 whitespace-pre-line wrap-anywhere">{{ decision.brief.instructions }}</p>
        </details>
        <p v-if="choice.judgement === 'judgement'" class="mt-2 text-xs text-muted">Needs your judgement.</p>
        <UButton class="mt-3" size="sm" :color="choice.id === decision.recommended_choice_id ? 'primary' : 'neutral'"
          :variant="choice.id === decision.recommended_choice_id ? 'solid' : 'outline'"
          :disabled="disabled || !!pendingChoice || choice.action === 'answer'" :loading="pendingChoice === choice.id"
          :aria-label="`Choose: ${choiceLabel(choice.action)}`" @click="choose(choice.id)">
          {{ pendingChoice === choice.id ? 'Submitting…' : 'Choose this option' }}
        </UButton>
      </li>
    </ol>
  </section>
</template>
