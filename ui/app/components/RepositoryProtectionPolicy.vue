<script setup lang="ts">
defineProps<{ mode: 'required' | 'optional'; busy: boolean }>()
defineEmits<{ change: [mode: 'required' | 'optional'] }>()
const id = useId()
</script>
<template>
  <section class="mt-5 rounded-xl border border-line bg-panel p-4 text-sm" :aria-labelledby="`${id}-heading`">
    <h2 :id="`${id}-heading`" class="font-semibold text-white">GitHub branch protection</h2>
    <p class="mt-1 text-muted">Forge checks Review, Audit, CI and the merge policy before merging its work.</p>
    <label :for="id" class="mt-3 block">Require protection on GitHub</label>
    <select :id="id" class="mt-2 rounded border border-line bg-panel p-2" :value="mode" :disabled="busy" @change="$emit('change', ($event.target as HTMLSelectElement).value as 'required' | 'optional')">
      <option value="required">Required</option>
      <option value="optional">Optional</option>
    </select>
    <p v-if="mode === 'required'" class="mt-2 text-xs text-muted">Repository setup requires GitHub branch rules. GitHub requires a paid plan for these rules on private repositories.</p>
    <p v-else class="mt-2 text-xs text-muted">Works with private repositories on GitHub Free. Other writers may merge without Forge’s checks when GitHub does not enforce them. Existing GitHub rules still apply.</p>
  </section>
</template>
