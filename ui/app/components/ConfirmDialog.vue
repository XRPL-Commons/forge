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
}>(), { reasonLabel: 'Decision or reason', requireReason: true, error: null, initialReason: '' })

const emit = defineEmits<{ confirm: [reason: string]; close: [] }>()
const reason = ref(props.initialReason)
const ready = computed(() => !props.requireReason || reason.value.trim().length > 0)

watch(() => props.open, (open) => { if (open) reason.value = props.initialReason })

function submit() {
  if (!ready.value) return
  emit('confirm', reason.value.trim())
}
</script>

<template>
  <UModal
    :open="open"
    :title="title"
    :description="text"
    :close="false"
    @update:open="value => !value && emit('close')"
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
      />
    </template>
    <template #footer>
      <div class="flex justify-end gap-2">
        <UButton color="neutral" variant="ghost" @click="emit('close')">Close</UButton>
        <UButton color="primary" :disabled="!ready" @click="submit">{{ confirmLabel }}</UButton>
      </div>
    </template>
  </UModal>
</template>
