<script setup lang="ts">
import { agentCapabilityLabels, type ConnectionRequest } from '~/composables/useAgentConnections'
useHead({ title: 'Agent connections' })
const route = useRoute()
const connections = useAgentConnections()
const code = ref(typeof route.query.code === 'string' ? route.query.code : '')
const request = ref<ConnectionRequest | null>(null)
const selectedCapabilities = ref<string[]>([])
const selectedRepositories = ref<number[]>([])
const selectedOwners = ref<number[]>([])
const confirmed = ref(false)
const busy = ref(false)
const mounted = ref(false)
onMounted(() => { mounted.value = true })
const message = ref('')
const error = ref('')
const localTime = useLocalTime()
const labels = agentCapabilityLabels
const hasScope = computed(() => selectedRepositories.value.length + selectedOwners.value.length > 0)
async function perform(action: () => Promise<void>) {
  busy.value = true; error.value = ''; message.value = ''
  try { await action() } catch (e) { error.value = e instanceof Error ? e.message : 'The connection could not be updated.' }
  finally { busy.value = false }
}
async function review() {
  request.value = null; confirmed.value = false
  await perform(async () => {
    const result = await connections.preview(code.value.trim().toUpperCase())
    request.value = result
    selectedCapabilities.value = [...result.capabilities]
    selectedRepositories.value = result.scope.repositories.map(r => r.id)
    selectedOwners.value = result.scope.owners.map(o => o.id)
  })
}
async function decide(decision: 'approve' | 'deny') {
  const value = request.value
  if (!value) return
  await perform(async () => {
    await connections.decide(value, decision, decision === 'deny' ? value.capabilities : selectedCapabilities.value,
      decision === 'deny' ? value.scope : {
        repositories: value.scope.repositories.filter(r => selectedRepositories.value.includes(r.id)),
        owners: value.scope.owners.filter(o => selectedOwners.value.includes(o.id)),
      })
    request.value = null; code.value = ''; confirmed.value = false
    message.value = decision === 'approve' ? 'Connection approved. Return to your agent to finish connecting.' : 'Connection denied.'
    await connections.refresh()
    await navigateTo('/settings/agents', { replace: true })
  })
}
async function revoke(id: string) {
  await perform(async () => { await connections.revoke(id); message.value = 'Connection revoked.'; await connections.refresh() })
}
</script>
<template>
  <div class="max-w-4xl space-y-6">
    <NuxtLink to="/settings/connections" class="mb-4 inline-block text-sm text-sky-200 hover:underline">← Connections</NuxtLink>
    <div><p class="text-xs uppercase tracking-wide text-muted">Settings</p><h1 class="text-3xl font-semibold">Agent connections</h1>
      <p class="mt-2 text-muted">Approve an external agent once for the repositories it may manage. Its GitHub credentials stay on its computer.</p></div>
    <UAlert v-if="error || connections.error.value" color="error" :description="error || connections.error.value || ''" />
    <p v-if="message" role="status" class="text-mint-200">{{ message }}</p>
    <p v-if="!connections.data.value" class="text-muted">Loading connections…</p>
    <UAlert v-else-if="!connections.data.value.enabled" color="warning" :description="connections.data.value.reason || 'Configure HTTPS and operator sign-in to connect an agent.'" />
    <template v-else>
      <section class="space-y-4 rounded-xl border border-line bg-panel p-5" aria-labelledby="review-title">
        <h2 id="review-title" class="text-xl font-semibold">Review a connection request</h2>
        <p class="text-sm text-muted">Enter the code shown by your local Forge client. Only approve a connection you started.</p>
        <form class="flex flex-wrap items-end gap-3" @submit.prevent="review">
          <div class="space-y-1"><label for="connection-code" class="block text-sm font-medium">Connection code</label><UInput id="connection-code" v-model="code" :disabled="!mounted" placeholder="BCDF-2345" autocomplete="off" maxlength="9" /></div>
          <UButton type="submit" :disabled="!mounted || busy || !code.trim()" :loading="busy">Review request</UButton>
        </form>
        <div v-if="request" class="space-y-4 border-t border-line pt-4">
          <h3 class="text-lg font-semibold">{{ request.name }}</h3>
          <p>Connecting to {{ request.origin }} · code <strong class="font-mono">{{ request.user_code }}</strong></p>
          <p class="text-sm text-muted">Expires {{ localTime(request.expires) }}. The client supplied its name and requested scope.</p>
          <fieldset class="space-y-2"><legend class="mb-2 font-medium">Allowed actions</legend>
            <UCheckbox v-for="capability in request.capabilities" :key="capability"
              :model-value="selectedCapabilities.includes(capability)" :label="labels[capability] || capability"
              :disabled="capability === 'repositories:read'" @update:model-value="checked => selectedCapabilities = checked ? [...selectedCapabilities, capability] : selectedCapabilities.filter(c => c !== capability)" />
          </fieldset>
          <fieldset class="space-y-2"><legend class="mb-2 font-medium">Repository scope</legend>
            <UCheckbox v-for="repo in request.scope.repositories" :key="repo.id" :model-value="selectedRepositories.includes(repo.id)"
              :label="`${repo.name} (repository ID ${repo.id})`" @update:model-value="checked => selectedRepositories = checked ? [...selectedRepositories, repo.id] : selectedRepositories.filter(id => id !== repo.id)" />
            <UCheckbox v-for="owner in request.scope.owners" :key="owner.id" :model-value="selectedOwners.includes(owner.id)"
              :label="`All current and future repositories owned by ${owner.login} (owner ID ${owner.id})`"
              @update:model-value="checked => selectedOwners = checked ? [...selectedOwners, owner.id] : selectedOwners.filter(id => id !== owner.id)" />
          </fieldset>
          <p v-if="selectedCapabilities.includes('repositories:activate')" class="text-sm text-muted">Enabling a repository can start work on its eligible issues.</p>
          <UCheckbox v-model="confirmed" label="I started this connection and the code matches my local client." />
          <div class="flex gap-3"><UButton :disabled="busy || !confirmed || !hasScope" @click="decide('approve')">Approve connection</UButton>
            <UButton color="neutral" variant="outline" :disabled="busy" @click="decide('deny')">Deny</UButton></div>
        </div>
      </section>
      <section class="space-y-4" aria-labelledby="connections-title">
        <h2 id="connections-title" class="text-xl font-semibold">Connected agents</h2>
        <p v-if="!connections.data.value.connections.length" class="text-muted">No agents connected yet.</p>
        <article v-for="client in connections.data.value.connections" :key="client.id" class="space-y-2 rounded-xl border border-line p-5">
          <div class="flex items-center justify-between gap-4"><h3 class="font-semibold">{{ client.name }} · {{ client.status }}</h3>
            <UButton v-if="client.status === 'active'" color="error" variant="outline" :disabled="!mounted || busy" @click="revoke(client.id)">Revoke access</UButton></div>
          <p class="text-sm text-muted">Approved by {{ client.operator.login }} · {{ localTime(client.created) }}</p>
          <ul class="list-inside list-disc text-sm"><li v-for="cap in client.capabilities" :key="cap">{{ labels[cap] || cap }}</li></ul>
          <p v-for="repo in client.scope.repositories" :key="repo.id" class="text-sm">{{ repo.name }} · repository {{ repo.id }}</p>
          <p v-for="owner in client.scope.owners" :key="owner.id" class="text-sm">All current and future repositories owned by {{ owner.login }} · owner {{ owner.id }}</p>
          <p class="text-sm text-muted">Last used: {{ client.last_used ? localTime(client.last_used) : 'Not yet used' }} · Expires {{ localTime(client.expires) }}</p>
        </article>
      </section>
    </template>
  </div>
</template>
