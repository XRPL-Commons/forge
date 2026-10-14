<script setup lang="ts">
import type { StewardDecisionMarker, StewardSelection } from '~/utils/steward'
import { forgeReportUrl } from '~/utils/issues'
const props = withDefaults(defineProps<{
  issueKey: string
  expected?: StewardDecisionMarker | null
  active?: boolean
  disabled?: boolean
}>(), { active: true, disabled: false, expected: null })
const emit = defineEmits<{ selected: [] }>()
const state = useStewardDecision(() => props.issueKey, { allowed: () => props.active, expected: () => props.expected })
const { decision, disabled: unavailable, pending, operation, message, resource, canRetry } = state
const reportUrl = computed(() => forgeReportUrl(resource.data.value?.report_draft))
async function retry() {
  if (!props.disabled && await state.retry()) emit('selected')
}
async function select(selection: StewardSelection) {
  if (!props.disabled && await state.select(selection)) emit('selected')
}
</script>

<template>
  <div class="space-y-3" data-steward-decision>
    <p v-if="!decision" class="text-sm text-muted" role="status">
      {{ resource.error.value ?? (resource.updatedAt.value ? 'No current steward decision.' : 'Loading choices…') }}
    </p>
    <RankedChoices v-if="decision" :decision="decision" :repositories="resource.data.value?.repositories" :scope="resource.data.value?.scope" :disabled="disabled || unavailable"
      :pending-choice="pending ? operation?.choice_id : null" :selected-choice="decision.application?.choice_id" @select="select" />
    <p v-if="message" role="status" class="text-sm text-muted" aria-live="polite">{{ message }}</p>
    <a v-if="reportUrl" :href="reportUrl" target="_blank" rel="noopener noreferrer" class="block text-sm text-sky-200 underline">Open the prepared Forge report ↗</a>
    <UButton v-if="canRetry" size="xs" color="primary" variant="outline" :disabled="disabled" @click="retry">
      Retry recording this choice
    </UButton>
    <UButton size="xs" color="neutral" variant="outline" :disabled="pending" @click="state.refresh()">
      Refresh choices
    </UButton>
  </div>
</template>
