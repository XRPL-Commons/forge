<script setup lang="ts">
defineProps<{ reason: string; url: string; canAssign: boolean }>()
const emit = defineEmits<{ close: []; assign: [] }>()
</script>

<template>
  <UModal :open="true" title="Approve the pull request" description="Review the changes on GitHub with your own account." @update:open="open => { if (!open) emit('close') }">
    <template #body>
      <div class="space-y-3 text-sm">
        <p class="font-medium">{{ reason }}</p>
        <p>Check this reason and the pull request’s changes before approving.</p>
        <p>Approval happens on GitHub with your own account. Use an authorized reviewer account that did not author the change. Forge never approves on your behalf.</p>
        <p>Forge merges automatically once GitHub records the approval and all gates still pass. You do not need to return to Forge.</p>
      </div>
    </template>
    <template #footer>
      <div class="flex flex-wrap gap-2">
        <UButton :to="url" external target="_blank" rel="noopener noreferrer" @click="emit('close')">Review and approve on GitHub</UButton>
        <UButton v-if="canAssign" color="neutral" variant="outline" @click="emit('assign')">Assign reviewer…</UButton>
      </div>
    </template>
  </UModal>
</template>
