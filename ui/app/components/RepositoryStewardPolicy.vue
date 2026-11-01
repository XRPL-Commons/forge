<script setup lang="ts">
const props = defineProps<{ repoId: number }>()
const api = useForgeApi()
interface Policy { ok: true; repo_id: number; name: string; policy: 'manual' | 'reversible'; revision: number }
const mounted = ref(false)
const saving = ref(false)
const resource = useResource<Policy>(() => `/api/repositories/steward?repo_id=${props.repoId}`,
  path => api.get<Policy>(path), { server: false, allowed: () => mounted.value && !saving.value })
const current = computed(() => resource.data.value)
onMounted(() => { mounted.value = true; resource.start() })
async function save(policy: string) {
  if (!current.value || resource.stale.value || saving.value) throw new Error('Refresh the recovery setting before saving.')
  const value = current.value
  saving.value = true
  try {
    const result = await api.act<Policy>('/api/repositories/steward', { repo_id: value.repo_id, revision: value.revision, policy })
    if (result.repo_id === props.repoId) resource.replace(result)
  } catch (failure) {
    saving.value = false
    await resource.refresh()
    throw failure
  } finally { saving.value = false }
}
</script>
<template>
  <div>
    <RepositoryChoiceSetting v-if="current" id="recovery-policy" label="Recovery" :value="current.policy"
      :options="[{ value: 'reversible', label: 'Handle routine recovery automatically' }, { value: 'manual', label: 'Ask before recovery actions' }]"
      :disabled="saving || resource.stale.value" :save="save">
      Forge can retry, wait for dependencies and rebuild when the next step is clear. Splitting, closing or combining issues still needs your approval.
    </RepositoryChoiceSetting>
    <p v-else role="status" class="text-sm text-muted">{{ resource.error.value || 'Loading recovery setting…' }}</p>
    <UButton v-if="resource.stale.value || resource.error.value" class="mt-2" color="neutral" variant="outline" :disabled="saving" @click="resource.refresh()">Refresh recovery setting</UButton>
  </div>
</template>
