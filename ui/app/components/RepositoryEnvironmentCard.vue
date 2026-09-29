<script setup lang="ts">
import type { RepositoryEnvironment } from '~/utils/repositories'

const props = defineProps<{
  environment: RepositoryEnvironment
  busy: boolean
  action: 'scan' | 'confirm' | 'automatic' | null
  error: string
  accessReady: boolean
}>()
defineEmits<{ scan: []; confirm: []; automatic: [] }>()
const scanning = computed(() => props.action === 'scan' || props.environment.status === 'running')
const names: Record<string, string> = { mongodb: 'MongoDB', postgres: 'PostgreSQL', redis: 'Redis' }
const databases = (services: string[]) => services.map(service => names[service] ?? service).join(', ')
const evidence = computed(() => props.environment.proposal ?? props.environment.profile)
const unavailable = computed(() => !!props.environment.proposal?.services.length && !props.environment.full_available)
</script>

<template>
  <section id="test-databases" class="mt-4 rounded-xl border border-line bg-panel p-4" aria-labelledby="test-databases-title">
    <h2 id="test-databases-title" class="text-sm font-semibold text-white">Test databases</h2>
    <p class="mt-1 text-sm text-muted">The supervisor chooses an environment for each issue using its description and automatic repository scans. No repository approval is needed.</p>

    <div class="mt-3 text-sm" role="status" aria-live="polite">
      <template v-if="environment.profile">
        <p class="font-medium text-mint-200">Repository default: {{ environment.profile.services.length ? databases(environment.profile.services) : 'Standard environment' }}</p>
        <p class="mt-1 text-muted">{{ environment.profile.services.length ? 'This optional default is used when the issue does not identify a more specific environment.' : 'The supervisor uses automatic selection for issues in this repository.' }}</p>
      </template>
      <p v-else class="text-muted">Automatic selection is enabled. Scans run when Forge assigns work; you can refresh the evidence here.</p>
      <p v-if="scanning" class="mt-2 text-sky-200">Scanning dependency, test and CI files…</p>
    </div>

    <div v-if="environment.proposal && !scanning" class="mt-3 rounded-lg border border-line p-3">
      <p class="text-sm font-medium text-white">{{ environment.proposal.services.length ? 'Detected: ' + databases(environment.proposal.services) : 'No supported databases detected' }}</p>
      <p class="mt-1 text-xs text-muted">{{ environment.proposal.services.length ? 'The supervisor can use this evidence immediately when an issue needs database tests.' : 'The supervisor starts with standard workers and can adjust if a worker reports a missing database.' }}</p>
      <p v-if="unavailable" role="alert" class="mt-2 text-sm text-amber-200">Database workers are unavailable on this host. Ask the Forge administrator to enable them before saving.</p>
      <details class="mt-3 text-xs text-muted">
        <summary class="cursor-pointer text-sky-200">Optional repository default</summary>
        <p class="mt-2">Use these databases as a fallback for issues without a specific requirement.</p>
        <UButton class="mt-2" :disabled="busy || unavailable || !accessReady" :loading="action === 'confirm'" @click="$emit('confirm')">Save repository default</UButton>
      </details>
    </div>

    <UButton v-if="environment.profile" class="mt-3" color="neutral" variant="outline" :disabled="busy || !accessReady" :loading="action === 'automatic'" @click="$emit('automatic')">Use automatic selection</UButton>

    <details v-if="evidence" class="mt-3 text-xs text-muted">
      <summary class="cursor-pointer text-sky-200">Source files</summary>
      <p class="mt-2">Scanned repository version {{ evidence.head.slice(0, 12) }}.</p>
      <ul v-if="evidence.evidence.length" class="mt-2 space-y-1">
        <li v-for="file in evidence.evidence" :key="file" class="break-words">{{ file }}</li>
      </ul>
      <p v-else class="mt-2">No supported database references found in the scanned files.</p>
    </details>

    <p v-if="error || environment.error" role="alert" class="mt-3 text-sm text-rose-200">{{ error || environment.error }}</p>
    <p v-if="!accessReady" class="mt-3 text-sm text-amber-200">Refresh GitHub access to scan or save database requirements.</p>
    <UButton class="mt-3" color="neutral" variant="outline" :disabled="busy || scanning || !accessReady" :loading="scanning" @click="$emit('scan')">
      {{ environment.profile || environment.proposal || environment.status === 'failed' ? 'Scan again' : 'Scan requirements' }}
    </UButton>
  </section>
</template>
