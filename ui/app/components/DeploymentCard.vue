<script setup lang="ts">
import type { DeploymentCardModel } from '~/utils/repositories'

// The Deployment card on Settings · Repository setup (spec "Operator UI").
// Every sentence comes from `deploymentCard()`; this only lays them out and
// reports the operator's decision. The domain is the one thing typed here, so
// it is the one piece of local state.
const props = defineProps<{ card: DeploymentCardModel; busy: boolean; error: string }>()
const emit = defineEmits<{ confirm: [domain: string]; reject: []; reread: [] }>()

const domain = ref(props.card.domain)
// A poll must not overwrite what the operator is typing, so the box follows
// the candidate only when the proposal itself changes.
watch(() => props.card.domain, (value) => { domain.value = value })
</script>

<template>
  <section id="deployment" class="mt-4 rounded-xl border border-line bg-panel p-4">
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <h2 class="text-sm font-semibold text-white">Deployment</h2>
        <p class="mt-0.5 text-xs text-muted">Forge follows a merge until it reaches production. It only reads; it never deploys.</p>
      </div>
      <UBadge id="deployment-status" :color="card.state === 'confirmed' ? 'primary' : card.state === 'failed' ? 'warning' : 'neutral'" variant="subtle">{{ card.status }}</UBadge>
    </div>

    <p v-if="card.sentence" id="deployment-sentence" class="mt-3 text-sm text-muted">{{ card.sentence }}</p>

    <ul v-if="card.lines.length" id="deployment-facts" class="mt-3 flex flex-col gap-1 text-xs text-muted">
      <li v-for="line in card.lines" :key="line" class="break-words">{{ line }}</li>
    </ul>

    <p v-if="card.exposure" id="deployment-exposure" class="mt-1 break-words text-xs text-muted">{{ card.exposure }}</p>

    <div v-if="card.showDomain" class="mt-3">
      <label for="deployment-domain" class="block text-xs text-muted">Domain Forge reads the deployed commit from</label>
      <UInput
        id="deployment-domain"
        v-model="domain"
        placeholder="app.example.org"
        autocomplete="off"
        spellcheck="false"
        class="mt-1 w-full max-w-sm"
        :disabled="busy"
      />
      <p class="mt-1 text-xs text-muted">A bare host name, without scheme or path. Leave it empty to track GitHub deployments only.</p>
    </div>

    <p v-if="card.observation" id="deployment-observation" class="mt-3 break-words text-xs text-muted">{{ card.observation }}</p>
    <p v-if="card.note" id="deployment-note" class="mt-1 text-xs text-sky-200">{{ card.note }}</p>
    <p v-if="card.error" id="deployment-error" role="alert" class="mt-2 break-words text-xs text-amber-200">{{ card.error }}</p>
    <UAlert v-if="error" id="deployment-failure" class="mt-2" role="alert" color="error" variant="subtle" :description="error" />

    <div class="mt-3 flex flex-wrap items-center gap-2">
      <UButton v-if="card.actions.confirm" id="deployment-confirm" size="sm" color="primary" :disabled="busy" @click="emit('confirm', domain.trim())">Confirm</UButton>
      <UButton v-if="card.actions.reject" id="deployment-reject" size="sm" color="neutral" variant="outline" :disabled="busy" @click="emit('reject')">Reject</UButton>
      <UButton v-if="card.actions.reread" id="deployment-reread" size="sm" color="neutral" variant="ghost" :disabled="busy" @click="emit('reread')">Re-read</UButton>
    </div>
  </section>
</template>
