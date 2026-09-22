<script setup lang="ts">
import {
  ACTION_EFFECTS, ACTION_LABELS, decisionNeedsReason, defaultReason,
  type IssueAction, type IssueDetail,
} from '~/utils/issues'

// The confirmation step of a decision: it derives every string from the copy
// maps and knows no endpoints. The detail route owns both POSTs, so this
// component only reports what the operator confirmed.
const props = defineProps<{ open: boolean; action: IssueAction; detail: IssueDetail; error?: string | null }>()
const emit = defineEmits<{ submit: [decision: { action: IssueAction; reason: string }]; close: [] }>()

const needsReason = computed(() => decisionNeedsReason(props.action))

// A reversible decision still records why it was taken, so an empty reason
// becomes "<label> confirmed by operator" rather than an empty ledger entry.
function confirm(reason: string) {
  emit('submit', { action: props.action, reason: reason.trim() || defaultReason(props.action) })
}
</script>

<template>
  <!-- Keyed by issue and action so opening a different decision starts from
       an empty reason instead of inheriting the previous one. -->
  <ConfirmDialog
    :key="`${detail.key}:${action}`"
    :open="open"
    :title="`${ACTION_LABELS[action]}?`"
    :text="ACTION_EFFECTS[action]"
    :confirm-label="ACTION_LABELS[action]"
    reason-label="What changed?"
    :require-reason="needsReason"
    :error="error"
    @confirm="confirm"
    @close="emit('close')"
  />
</template>
