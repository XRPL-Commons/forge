<script setup lang="ts">
import type { MirrorDeniedRepository } from '~/utils/repositories'
const props = defineProps<{ entries: MirrorDeniedRepository[]; busy: boolean; error: string }>()
const emit = defineEmits<{ save: [entries: MirrorDeniedRepository[]] }>()
const format = (entries: MirrorDeniedRepository[]) => entries.map(entry => entry.canonical_name + (entry.repository_id ? ` ${entry.repository_id}` : '')).join('\n')
const draft = ref(format(props.entries))
const saved = computed(() => format(props.entries))
const invalid = ref('')
watch(saved, (value, previous) => { if (draft.value === previous) draft.value = value })
function save() {
  invalid.value = ''
  const entries: MirrorDeniedRepository[] = []
  for (const line of draft.value.split('\n').map(value => value.trim()).filter(Boolean)) {
    const match = /^([A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+)(?:\s+([1-9][0-9]*))?$/.exec(line)
    if (!match || (match[2] && !Number.isSafeInteger(Number(match[2])))) {
      invalid.value = 'Use owner/repository, optionally followed by its numeric GitHub repository ID, on each line.'
      return
    }
    entries.push({ canonical_name: match[1]!, ...(match[2] ? { repository_id: Number(match[2]) } : {}) })
  }
  emit('save', entries)
}
</script>
<template>
  <section class="mt-5 rounded-xl border border-line bg-panel p-4 text-sm" aria-labelledby="mirror-policy-heading">
    <h2 id="mirror-policy-heading" class="font-semibold text-white">Git dependency restrictions</h2>
    <p class="mt-1 text-muted">Block sensitive repositories from this project's workers, including declared dependencies. Restrictions apply to future fetches immediately. Removing one restores access only when the assignment policy and GitHub App allow it.</p>
    <label for="mirror-denied" class="mt-3 block">Blocked repositories — one owner/repository per line</label>
    <p id="mirror-denied-help" class="mt-1 text-xs text-muted">Optionally add the numeric GitHub repository ID after a space to keep blocking it after a rename. An empty list removes these project restrictions.</p>
    <textarea id="mirror-denied" v-model="draft" :disabled="busy" aria-describedby="mirror-denied-help" rows="4" class="mt-2 w-full rounded border border-line bg-panel p-2 font-mono text-xs" />
    <p v-if="invalid || error" role="alert" class="mt-2 text-amber-200">{{ invalid || error }}</p>
    <UButton class="mt-2" :disabled="busy || draft === saved" @click="save">Save restrictions</UButton>
  </section>
</template>
