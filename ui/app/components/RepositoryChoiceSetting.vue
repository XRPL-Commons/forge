<script setup lang="ts">
import { actionMessage } from '~/utils/repositories'
const props = defineProps<{
  id: string
  label: string
  value: string
  options: { value: string; label: string }[]
  disabled?: boolean
  save: (value: string) => Promise<unknown>
}>()
const mounted = ref(false)
onMounted(() => { mounted.value = true })
const draft = ref(props.value)
const saving = ref(false)
const error = ref('')
const saved = ref(false)
watch(() => props.value, (value, previous) => { if (draft.value === previous) draft.value = value })
watch(draft, () => { saved.value = false; error.value = '' })
async function submit() {
  if (!mounted.value || saving.value || props.disabled || draft.value === props.value) return
  saving.value = true; saved.value = false; error.value = ''
  try { await props.save(draft.value); saved.value = true }
  catch (failure) { error.value = actionMessage(failure, 'Could not save. Check the current setting before trying again.') }
  finally { saving.value = false }
}
</script>
<template>
  <form class="space-y-2" @submit.prevent="submit">
    <label :for="id" class="block font-medium text-white">{{ label }}</label>
    <div class="flex flex-wrap items-center gap-3">
      <USelectMenu
        :id="id" v-model="draft" :items="options" value-key="value" :search-input="false"
        :disabled="!mounted || disabled || saving" :aria-label="label" :aria-describedby="id + '-help'"
        class="w-full min-w-0 sm:w-96" :ui="{ value: 'whitespace-normal text-left', itemLabel: 'whitespace-normal' }"
      />
      <template v-if="draft !== value">
        <UButton type="submit" color="neutral" variant="outline" :aria-label="`Save ${label.toLowerCase()}`" :disabled="!mounted || disabled || saving" :loading="saving">Save</UButton>
        <UButton type="button" color="neutral" variant="ghost" :aria-label="`Discard ${label.toLowerCase()} changes`" :disabled="saving" @click="draft = value">Discard</UButton>
      </template>
      <span v-if="saved" role="status" class="text-xs text-mint-200">Saved</span>
    </div>
    <div :id="id + '-help'" class="text-sm text-muted"><slot :value="draft" /></div>
    <p v-if="error" role="alert" class="text-sm text-amber-200">{{ error }}</p>
  </form>
</template>
