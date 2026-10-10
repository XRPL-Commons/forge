<script setup lang="ts">
import { ACTION_LABELS, ACTION_PENDING_LABELS, type IssueAction } from '~/utils/issues'

// Dropping an issue is the one decision that cannot be undone from Forge, so
// its dialog says exactly what happens on GitHub and asks why. Like
// DecisionPanel it knows no endpoints: the detail route submits it.
// `reason` prefills the box when a discussion proposed the close; the
// operator still reads it and confirms.
const props = defineProps<{ open: boolean; issueKey: string; number: number; pull?: number | null; error?: string | null; pending?: boolean; reason?: string }>()
const emit = defineEmits<{ submit: [decision: { action: IssueAction; reason: string }]; close: [] }>()

const reason = ref('')
watch(() => props.open, (open) => { if (open) reason.value = props.reason ?? '' }, { immediate: true })
const ready = computed(() => reason.value.trim().length > 0)

function submit() {
  if (ready.value && !props.pending) emit('submit', { action: 'close', reason: reason.value.trim() })
}
function dismiss() {
  if (!props.pending) emit('close')
}
</script>

<template>
  <UModal
    :key="issueKey"
    :open="open"
    :title="`Close issue #${number}?`"
    description="Use this when Forge should not finish this issue."
    :close="false"
    :dismissible="!pending"
    @update:open="value => !value && dismiss()"
  >
    <template #body>
      <UAlert v-if="error" class="mb-3" role="alert" color="error" variant="subtle" :description="error" />
      <ul id="close-effects" class="list-disc space-y-1 pl-5 text-sm text-muted">
        <li>Forge stops any running work on this issue.</li>
        <li>{{ pull ? `Forge closes its pull request, PR #${pull}.` : 'Forge closes its pull request, if it has one.' }}</li>
        <li>Forge comments your reason on the issue and closes it on GitHub as “not planned”.</li>
      </ul>
      <p class="mt-3 text-sm text-muted">
        This does not delete the issue: GitHub only lets repository admins delete issues. You can reopen it on GitHub later.
      </p>
      <label for="close-reason" class="mt-4 block text-sm text-muted">Why are you closing it?</label>
      <UTextarea
        id="close-reason"
        v-model="reason"
        aria-label="Why are you closing it?"
        :rows="3"
        class="mt-1 w-full"
        required
        :disabled="pending"
      />
    </template>
    <template #footer>
      <DialogFooter>
        <template #secondary>
          <UButton color="neutral" variant="ghost" :disabled="pending" @click="dismiss">Cancel</UButton>
        </template>
        <template #primary>
          <UButton color="error" data-confirm :loading="pending" :disabled="!ready || pending" :aria-busy="pending" @click="submit">{{ pending ? ACTION_PENDING_LABELS.close : ACTION_LABELS.close }}</UButton>
        </template>
      </DialogFooter>
    </template>
  </UModal>
</template>
