<script setup lang="ts">
const props = defineProps<{ repoId: number }>()
const api = useForgeApi()
interface Policy { ok: true; repo_id: number; name: string; policy: 'manual' | 'reversible'; revision: number }
const mounted = ref(false)
const saving = ref(false)
const error = ref('')
const resource = useResource<Policy>(() => `/api/repositories/steward?repo_id=${props.repoId}`,
  path => api.get<Policy>(path), { server: false, allowed: () => mounted.value && !saving.value })
const current = computed(() => resource.data.value)
onMounted(() => { mounted.value = true; resource.start() })
async function save(policy: Policy['policy']) {
  if (!current.value || resource.stale.value || saving.value) return
  const value = current.value
  saving.value = true; error.value = ''
  try {
    const result = await api.act<Policy>('/api/repositories/steward', { repo_id: value.repo_id, revision: value.revision, policy })
    if (result.repo_id === props.repoId) resource.replace(result)
  } catch {
    error.value = 'Could not confirm the change. Refresh to see the saved policy before trying again.'
  } finally { saving.value = false }
}
</script>
<template>
  <section class="mt-5 rounded-xl border border-line bg-panel p-4 text-sm" aria-labelledby="steward-policy-title">
    <h2 id="steward-policy-title" class="font-semibold text-white">Steward autonomy</h2>
    <p class="mt-1 text-muted">Choose how Forge handles retries, waits and rebuilds for this repository. Splitting, closing or folding issues always needs your choice.</p>
    <p v-if="current" class="mt-3" role="status">{{ current.policy === 'manual' ? 'Ask before every steward move.' : 'Apply obvious retries, waits and rebuilds automatically.' }}</p>
    <p v-else class="mt-3" role="status">{{ resource.error.value || 'Loading policy…' }}</p>
    <UButton v-if="current" class="mt-3" :disabled="saving || resource.stale.value" @click="save(current.policy === 'manual' ? 'reversible' : 'manual')">
      {{ current.policy === 'manual' ? 'Allow obvious reversible moves' : 'Ask before every move' }}
    </UButton>
    <p v-if="error" role="alert" class="mt-2 text-amber-200">{{ error }}</p>
    <UButton v-if="error || resource.stale.value || resource.error.value" class="mt-2" color="neutral" variant="outline" :disabled="saving" @click="resource.refresh()">Refresh policy</UButton>
  </section>
</template>
