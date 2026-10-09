<script setup lang="ts">
defineProps<{ reason: string; url: string; canAssign: boolean; verification?: boolean }>()
const emit = defineEmits<{ close: []; assign: []; opened: [] }>()
</script>

<template>
  <UModal :open="true" title="Approve the pull request" description="Review the changes on GitHub with your own account." @update:open="open => { if (!open) emit('close') }">
    <template #body>
      <div class="space-y-3 text-sm">
        <p class="whitespace-pre-wrap font-medium">{{ reason }}</p>
        <p>Check this reason and the pull request’s changes before approving.</p>
        <p>Approval happens on GitHub with your own account. Use an authorized reviewer account that did not author the change. Forge never approves on your behalf.</p>
        <p v-if="verification">Run the requested check before approving. Your approval resumes the remaining reviews; Forge merges when every check passes.</p>
        <p v-else>Forge merges automatically once GitHub records the approval and all gates still pass. You do not need to return to Forge.</p>
      </div>
    </template>
    <template #footer>
      <DialogFooter>
        <template v-if="canAssign" #secondary>
          <UButton color="neutral" variant="outline" @click="emit('assign')">Assign reviewer…</UButton>
        </template>
        <template #primary>
          <UButton :to="url" external target="_blank" rel="noopener noreferrer" @click="emit('opened'); emit('close')">Review and approve on GitHub</UButton>
        </template>
      </DialogFooter>
    </template>
  </UModal>
</template>
