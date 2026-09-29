<script setup lang="ts">
const localTime = useLocalTime()
import { checkedAtText } from '~/utils/repositories'
const props = defineProps<{ repoId: number }>()
const repoId = computed(() => props.repoId)
const route = useRoute()
const wizard = ref(route.query.setup === '1')
watch(() => route.query.setup, value => { if (value === '1') wizard.value = true })
const { poll, live, row, report, shown, displayReady, ready, prepared, failed, hint, editUrl, downloadHref, downloadAriaDisabled, downloaded, busy, checking, error, deployment, deployError, environmentError, environmentAction, rejecting, check, decide, rejectDeployment, updateEnvironment } = useRepository(repoId, computed(() => !wizard.value))
async function closed() { wizard.value = false; await navigateTo(`/settings/repositories/${props.repoId}`, { replace: true }); await poll.refresh() }
</script>
<template>
  <div>
    <NuxtLink to="/settings/repositories" class="text-sm text-sky-200">← Repositories</NuxtLink>
    <h1 class="mt-3 break-all text-3xl font-semibold text-white">{{ row?.name || 'Repository' }}</h1>
    <p v-if="error" id="repo-error" role="alert" class="mt-3 text-sm text-rose-200">{{ error }}</p>
    <template v-if="row">
      <div class="mt-4 flex flex-wrap items-center gap-3">
        <UButton @click="wizard = true">{{ row.enabled && ready ? 'Review setup' : 'Continue setup' }}</UButton>
        <UButton color="neutral" variant="outline" :disabled="busy || !row.access_ready" :loading="checking" @click="check">Refresh repository</UButton>
        <span id="repo-checked-at" class="text-xs text-muted">{{ checkedAtText(report, localTime) }}</span>
      </div>
      <RepositoryRulesPanel id-prefix="repo-" :row="row" :live="live" :report="report" :display-ready="displayReady" :shown="shown" :failed="failed" :hint="hint" :prepared="prepared" :edit-url="editUrl" :download-href="downloadHref" :download-aria-disabled="downloadAriaDisabled" :downloaded="downloaded" @download="downloaded = true" />
      <DeploymentCard
        v-if="deployment"
        :card="deployment"
        :busy="busy"
        :error="deployError"
        @confirm="domain => decide('confirm', { domain })"
        @reject="rejecting = true"
        @reread="decide('reread')"
      />

      <RepositoryEnvironmentCard
        v-if="live && row.environment"
        :key="row.id"
        :environment="row.environment"
        :busy="busy"
        :action="environmentAction"
        :error="environmentError"
        :access-ready="row.access_ready"
        @scan="updateEnvironment('scan')"
        @confirm="updateEnvironment('confirm')"
        @automatic="updateEnvironment('automatic')"
      />

    </template>
    <RepositorySetupWizard v-if="wizard" :initial-repo-id="repoId" @close="closed" @enabled="closed" />
    <ConfirmDialog
      :open="rejecting"
      :title="row ? `Reject the deployment reading for ${row.name}?` : ''"
      text="Forge keeps following this repository's issues. Nothing is saved to settings, and the reading can be requested again."
      confirm-label="Reject"
      reason-label="Why is this reading wrong?"
      :error="deployError"
      @confirm="rejectDeployment"
      @close="rejecting = false"
    />  </div>
</template>
