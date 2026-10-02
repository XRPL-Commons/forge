<script setup lang="ts">
import type { IssueAction } from '~/utils/issues'

// Dropping an issue is the one decision that cannot be undone from Forge, so
// its dialog says exactly what happens on GitHub and asks why. Like
// DecisionPanel it knows no endpoints: the detail route submits it.
const props = defineProps<{ open: boolean; issueKey: string; number: number; pull?: number | null; error?: string | null }>()
const emit = defineEmits<{ submit: [decision: { action: IssueAction; reason: string }]; close: [] }>()

const reason = ref('')
watch(() => props.open, (open) => { if (open) reason.value = '' }, { immediate: true })
const ready = computed(() => reason.value.trim().length > 0)

function submit() {
  if (ready.value) emit('submit', { action: 'close', reason: reason.value.trim() })
}
</script>

<template>
  <UModal
    :key="issueKey"
    :open="open"
    :title="`Close issue #${number}?`"
    description="Use this when Forge should not finish this issue."
    :close="false"
    @update:open="value => !value && emit('close')"
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
      />
    </template>
    <template #footer>
      <div class="flex w-full justify-end gap-2">
        <UButton color="neutral" variant="ghost" @click="emit('close')">Cancel</UButton>
        <UButton color="error" :disabled="!ready" @click="submit">Close issue</UButton>
      </div>
    </template>
  </UModal>
</template>
