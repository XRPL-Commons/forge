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
}>(), { reasonLabel: 'Decision or reason', requireReason: true, error: null })

const emit = defineEmits<{ confirm: [reason: string]; close: [] }>()
const reason = ref('')
const ready = computed(() => !props.requireReason || reason.value.trim().length > 0)

watch(() => props.open, (open) => { if (open) reason.value = '' })

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
      <p v-if="error" class="mb-3 rounded-lg border border-rose-200/40 px-3 py-2 text-sm text-rose-200" role="alert">{{ error }}</p>
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
