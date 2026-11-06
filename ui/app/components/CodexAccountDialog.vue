<script setup lang="ts">
import type { Harness } from '~/utils/onboarding'
interface AccountView {
  generation: string; revision: number; setup_revision: string
  account: { status: string; code?: string; url?: string; expires_at?: number }
  operation: { status?: string; error?: string; verification?: boolean }; active: boolean
}
const props = defineProps<{ harness: Harness; reconnect?: boolean; open: boolean }>()
const emit = defineEmits<{ close: [] }>()
const api = useForgeApi()
const busy = ref(false)
const requested = ref(false)
const error = ref('')
const poll = useResource<AccountView>('/api/accounts', path => api.get<AccountView>(path), {
  interval: 3000, allowed: () => props.open && !busy.value,
})
onMounted(() => poll.start())
const view = poll.data
const running = computed(() => ['queued', 'connecting', 'verifying'].includes(view.value?.operation.status ?? ''))
const failed = computed(() => view.value?.operation.status === 'failed' || view.value?.account.status === 'account_login_failed')
watch(() => view.value?.operation.status, status => {
  if (status && ['queued', 'connecting', 'verifying'].includes(status)) requested.value = true
})
const complete = computed(() => view.value?.operation.status === 'complete' && requested.value)
const message = computed(() => {
  if (view.value?.operation.status === 'verifying') return 'Verifying Codex…'
  if (complete.value) return 'Codex is connected and verified.'
  if (failed.value) return view.value?.operation.error || 'Sign-in expired or did not complete. Try again.'
  if (view.value?.account.code) return 'Enter this code on the Codex sign-in page.'
  if (running.value) return 'Preparing sign-in…'
  return 'Connect your Codex account to run work in this factory.'
})
async function act(action: string) {
  if (!view.value) return
  busy.value = true; error.value = ''; requested.value = true
  try {
    poll.replace(await api.act<AccountView>('/api/accounts', {
      action, provider: 'codex', generation: view.value.generation, revision: view.value.revision,
      setup_revision: view.value.setup_revision,
    }))
  } catch (failure) { error.value = failure instanceof Error ? failure.message : 'Could not connect. Try again.' }
  finally { busy.value = false }
}
const action = computed(() => failed.value ? (view.value?.operation.verification ? 'verify' : props.harness.connected ? 'reconnect' : 'retry')
  : props.reconnect ? 'reconnect' : props.harness.connected ? 'verify' : 'connect')
</script>
<template>
  <UModal :open="open" title="Connect Codex" :close="false" @update:open="value => !value && emit('close')">
    <template #body>
      <div class="space-y-4">
        <p role="status">{{ view ? message : 'Loading connection…' }}</p>
        <p v-if="error || poll.error.value" role="alert" class="text-error">{{ error || poll.error.value }}</p>
        <template v-if="view?.account.code && running">
          <p class="text-2xl font-mono tracking-wider break-all" data-device-code>{{ view.account.code }}</p>
          <p v-if="view.account.expires_at" class="text-sm text-muted">Expires {{ new Date(view.account.expires_at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) }}.</p>
          <a :href="view.account.url" target="_blank" rel="noopener noreferrer" class="inline-flex min-h-11 items-center rounded-full bg-mint-200 px-5 py-2 font-medium text-bg">Open Codex sign-in</a>
        </template>
        <p v-if="running" class="text-sm text-muted">You can close this window and return to follow progress.</p>
        <p v-else-if="!complete" class="text-sm text-muted">Sign in with OpenAI. Forge stores the connection on this factory; no credentials are entered here.</p>
        <UButton v-if="view && !running && !complete" :loading="busy" :disabled="!view.active" @click="act(action)">{{ action === 'verify' ? 'Verify connection' : failed ? 'Try again' : 'Connect Codex' }}</UButton>
      </div>
    </template>
    <template #footer><DialogFooter><template #secondary><UButton color="neutral" variant="ghost" @click="emit('close')">Close</UButton></template></DialogFooter></template>
  </UModal>
</template>
