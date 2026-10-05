<script setup lang="ts">
const props = withDefaults(defineProps<{
  open: boolean
  title: string
  text: string
  confirmLabel: string
  reasonLabel?: string
  requireReason?: boolean
  // Shown inside the dialog when the action was refused, so the operator can
  // correct it without losing what they typed.
  error?: string | null
  // Prefills the reason, e.g. with the reason a discussion proposed.
  initialReason?: string
  // True while the confirmed request is in flight: the button spins, nothing
  // can be submitted twice, and the dialog stays open until Forge answers.
  pending?: boolean
  pendingLabel?: string
}>(), { reasonLabel: 'Decision or reason', requireReason: true, error: null, initialReason: '', pending: false, pendingLabel: '' })

const emit = defineEmits<{ confirm: [reason: string]; close: [] }>()
const reason = ref(props.initialReason)
const ready = computed(() => !props.requireReason || reason.value.trim().length > 0)

watch(() => props.open, (open) => { if (open) reason.value = props.initialReason })

function submit() {
  if (!ready.value || props.pending) return
  emit('confirm', reason.value.trim())
}
function dismiss() {
  if (!props.pending) emit('close')
}
</script>

<template>
  <UModal
    :open="open"
    :title="title"
    :description="text"
    :close="false"
    :dismissible="!pending"
    @update:open="value => !value && dismiss()"
  >
    <template #body>
      <UAlert v-if="error" class="mb-3" role="alert" color="error" variant="subtle" :description="error" />
      <label :for="'confirm-reason'" class="block text-sm text-muted">{{ reasonLabel }}</label>
      <UTextarea
        id="confirm-reason"
        v-model="reason"
        :aria-label="reasonLabel"
        :rows="3"
        class="mt-1 w-full"
        :required="requireReason"
        :disabled="pending"
      />
    </template>
    <template #footer>
      <DialogFooter>
        <template #secondary>
          <UButton color="neutral" variant="ghost" :disabled="pending" @click="dismiss">Close</UButton>
        </template>
        <template #primary>
          <UButton
            color="primary"
            data-confirm
            :loading="pending"
            :disabled="!ready || pending"
            :aria-busy="pending"
            @click="submit"
          >{{ pending && pendingLabel ? pendingLabel : confirmLabel }}</UButton>
        </template>
      </DialogFooter>
    </template>
  </UModal>
</template>
