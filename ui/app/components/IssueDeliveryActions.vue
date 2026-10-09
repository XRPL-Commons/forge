<script setup lang="ts">
import type { IssueDetail } from '~/utils/issues'
import type { DeliveryAction } from '~/composables/useDeliveryDecision'
const props = defineProps<{ detail: IssueDetail }>()
const emit = defineEmits<{ submitted: [] }>()
const { preview, request, load, begin, apply } = useDeliveryDecision(() => props.detail)
const open = ref(false)
const action = ref<DeliveryAction | null>(null)
const receipt = ref('')
const labels = { drop: 'Remove from delivery', close_member: 'Close this issue', dissolve: 'Dissolve delivery', resume_delivery: 'Resume delivery' }
const dependentNumbers = computed(() => preview.value?.delivery?.members.filter(member => preview.value?.delivery?.dependent_members.includes(member.member_id)).map(member => `#${member.number}`).join(', '))
const explanation = computed(() => {
  if (action.value === 'resume_delivery') return 'Resume the whole delivery after verified worker cleanup. Each member keeps its own holds, backlog decision and environment requirements. The shared failure budget does not reset.'
  const base = action.value === 'dissolve'
    ? 'Stop this delivery and release every open member as a separate queued delivery with a new failure budget.'
    : action.value === 'close_member'
      ? 'Close only this issue and remove it from the delivery. The remaining members keep their shared failure budget.'
      : 'Release this issue as a separate queued delivery. Both deliveries retain the existing shared failure charges.'
  const dependencies = action.value !== 'dissolve' && dependentNumbers.value
    ? ` ${dependentNumbers.value} will wait for this issue as an external prerequisite.` : ''
  return `${base} All batch workers stop and finish cleanup first. Existing pull requests stay open and history is retained.${dependencies}`
})
watch(open, value => { if (value) void load() })
watch(() => props.detail.revision, () => { if (open.value && !action.value) void load() })
function choose(value: DeliveryAction) { begin(); action.value = value }
async function confirm(reason: string) {
  if (action.value && await apply(action.value, reason)) {
    action.value = null
    receipt.value = 'Decision queued. Waiting for verified batch cleanup.'
    emit('submitted')
  }
}
</script>
<template>
  <IssueSection id="issue-delivery-actions" v-model:open="open" title="Grouped delivery">
    <p v-if="request.error.value" role="alert" class="text-sm text-amber-200">{{ request.error.value }}</p>
    <p v-if="receipt" role="status" class="text-sm text-muted">{{ receipt }}</p>
    <template v-if="preview?.delivery">
      <p class="text-sm text-muted">Members: {{ preview.delivery.members.map(member => `#${member.number}`).join(', ') }}. One builder, one review and one audit share this delivery.</p>
      <p v-if="preview.delivery.pending_decision" class="mt-1 text-sm text-muted">A delivery decision is waiting to finish.</p>
      <div class="mt-2 flex flex-wrap gap-2">
        <UButton v-if="preview.delivery.unit_paused" size="sm" color="neutral" variant="outline" :disabled="request.pending.value || preview.delivery.pending_decision" @click="choose('resume_delivery')">Resume delivery</UButton>
        <UButton v-if="detail.state === 'open' && preview.delivery.member_count > 1" size="sm" color="neutral" variant="outline" :disabled="request.pending.value || preview.delivery.pending_decision" @click="choose('drop')">Remove from delivery</UButton>
        <UButton size="sm" color="neutral" variant="outline" :disabled="request.pending.value || preview.delivery.pending_decision" @click="choose('close_member')">{{ detail.state === 'closed' ? 'Remove closed issue' : 'Close this issue' }}</UButton>
        <UButton v-if="preview.delivery.member_count > 1" size="sm" color="neutral" variant="outline" :disabled="request.pending.value || preview.delivery.pending_decision" @click="choose('dissolve')">Dissolve delivery</UButton>
      </div>
    </template>
    <p v-else-if="request.pending.value" class="text-sm text-muted">Loading membership…</p>
    <ConfirmDialog v-if="action" :open="true" :title="labels[action]" :text="explanation" :confirm-label="labels[action]" :pending="request.pending.value" :error="request.error.value" @confirm="confirm" @close="action = null" />
  </IssueSection>
</template>
