<script setup lang="ts">
import type { RepositoryEnvironment } from '~/utils/repositories'

const props = defineProps<{
  environment: RepositoryEnvironment
  busy: boolean
  action: 'scan' | 'confirm' | null
  error: string
  accessReady: boolean
}>()
defineEmits<{ scan: []; confirm: [] }>()
const scanning = computed(() => props.action === 'scan' || props.environment.status === 'running')
const names: Record<string, string> = { mongodb: 'MongoDB', postgres: 'PostgreSQL', redis: 'Redis' }
const databases = (services: string[]) => services.map(service => names[service] ?? service).join(', ')
const evidence = computed(() => props.environment.proposal ?? props.environment.profile)
const unavailable = computed(() => !!props.environment.proposal?.services.length && !props.environment.full_available)
</script>

<template>
  <section id="test-databases" class="mt-4 rounded-xl border border-line bg-panel p-4" aria-labelledby="test-databases-title">
    <h2 id="test-databases-title" class="text-sm font-semibold text-white">Test databases</h2>
    <p class="mt-1 text-sm text-muted">Save this repository's database requirements once. Forge reuses them for future jobs. Scan again when its dependencies or test setup change.</p>

    <div class="mt-3 text-sm" role="status" aria-live="polite">
      <template v-if="environment.profile">
        <p class="font-medium text-mint-200">Saved: {{ environment.profile.services.length ? databases(environment.profile.services) : 'Standard environment' }}</p>
        <p class="mt-1 text-muted">{{ environment.profile.services.length ? 'Forge will make these databases available for future jobs. Issue environment overrides still apply.' : 'No test databases are configured for this repository.' }}</p>
      </template>
      <p v-else class="text-muted">No requirements saved yet. Scan this repository to detect the databases its tests use.</p>
      <p v-if="scanning" class="mt-2 text-sky-200">Scanning dependency, test and CI files…</p>
    </div>

    <div v-if="environment.proposal && !scanning" class="mt-3 rounded-lg border border-line p-3">
      <p class="text-sm font-medium text-white">{{ environment.proposal.services.length ? 'Detected: ' + databases(environment.proposal.services) : 'No supported databases detected' }}</p>
      <p class="mt-1 text-xs text-muted">{{ environment.proposal.services.length ? 'Review the source files below, then save these requirements.' : 'Save to use the standard environment. This scan checks for MongoDB, PostgreSQL and Redis.' }}</p>
      <p v-if="unavailable" role="alert" class="mt-2 text-sm text-amber-200">Database workers are unavailable on this host. Ask the Forge administrator to enable them before saving.</p>
      <UButton class="mt-3" :disabled="busy || unavailable || !accessReady" :loading="action === 'confirm'" @click="$emit('confirm')">Save requirements</UButton>
    </div>

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
