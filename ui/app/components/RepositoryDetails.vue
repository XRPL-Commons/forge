<script setup lang="ts">
const localTime = useLocalTime()
import { checkedAtText, rowState } from '~/utils/repositories'
const props = defineProps<{ repoId: number }>()
const repoId = computed(() => props.repoId)
const route = useRoute()
const deploymentOpen = ref(false)
const wizard = ref(route.query.setup === '1')
watch(() => route.query.setup, value => { if (value === '1') wizard.value = true })
const { poll, live, row, report, displayReady, ready, hint, busy, checking, error, deployment, deployError, environmentError, environmentNotice, environmentAction, rejecting, check, decide, rejectDeployment, updateEnvironment, mirrorError, mirrorNotice, updateMirrorPolicy, updateReviewScope, updateMergePolicy, disable } = useRepository(repoId, computed(() => !wizard.value), deploymentOpen)
const disabling = ref(false)
async function confirmDisable() { await disable(); if (!row.value?.enabled) disabling.value = false }
const environmentSummary = computed(() => row.value?.environment?.profile?.services.length
  ? row.value.environment.profile.services.join(', ') : 'Automatic')
async function closed() { wizard.value = false; await navigateTo(`/settings/repositories/${props.repoId}`, { replace: true }); await poll.refresh() }
</script>
<template>
  <div>
    <NuxtLink to="/settings/repositories" class="text-sm text-sky-200">← Repositories</NuxtLink>
    <h1 class="mt-3 break-all text-3xl font-semibold text-white">{{ row?.name || 'Repository' }}</h1>
    <UAlert v-if="error" id="repo-error" class="mt-3" role="alert" color="error" variant="subtle" :description="error" />
    <template v-if="row">
      <div class="mt-4 flex flex-wrap items-center gap-3">
        <UBadge :color="rowState(row).icon === 'attention' ? 'warning' : row.enabled ? 'success' : 'neutral'" variant="subtle">{{ rowState(row).label }}</UBadge>
        <UButton v-if="!displayReady || !row.access_ready" @click="wizard = true">Continue setup</UButton>
        <UButton color="neutral" variant="outline" to="/">View activity</UButton>
        <NuxtLink :to="`/planning/${repoId}`" class="text-sm text-sky-200">Plan work →</NuxtLink>
      </div>
      <p v-if="hint" class="mt-3 text-sm text-amber-200" role="status">{{ hint }}</p>
      <section class="mt-5 space-y-6 rounded-xl border border-line bg-panel p-5 text-sm" aria-labelledby="automation-heading">
        <h2 id="automation-heading" class="text-lg font-semibold text-white">Automation</h2>
        <div v-if="live">
          <USwitch :model-value="row.enabled" label="Enable automation" :disabled="busy" @update:model-value="value => value ? wizard = true : disabling = true" />
          <p class="mt-2 text-muted">Let Forge pick up eligible issues and review pull requests in this repository.</p>
        </div>
        <RepositoryChoiceSetting v-if="live && row.configured && row.merge_policy && row.merge_check_revision" id="merge-policy" label="Merging" :value="row.merge_policy"
          :options="[{ value: 'human', label: 'Require human approval' }, { value: 'auto', label: 'Merge automatically after checks' }]" :disabled="busy" :save="updateMergePolicy" v-slot="{ value }">
          <p>Forge merges its work after CI, independent Review and Audit pass. Any additional approval requested during the work must also be resolved.</p>
          <p v-if="value === 'human'" class="mt-1">An authorized person must also approve the pull request on GitHub. <NuxtLink to="/settings#approvers" class="text-sky-200 underline">See who can approve</NuxtLink>.</p>
        </RepositoryChoiceSetting>
        <RepositoryChoiceSetting v-if="live && row.configured && row.merge_check_revision" id="review-scope" label="Review pull requests" :value="row.merge_check_scope ?? 'all'"
          :options="[{ value: 'all', label: 'All pull requests' }, { value: 'forge', label: 'Forge-created pull requests only' }]" :disabled="busy" :save="updateReviewScope" v-slot="{ value }">
          <p>{{ value === 'forge' ? 'Forge reviews and audits only the pull requests it creates.' : 'Forge reviews and audits both its own pull requests and those opened by others.' }} Reviewing someone else's pull request does not authorize Forge to merge it.</p>
        </RepositoryChoiceSetting>
        <RepositoryStewardPolicy v-if="live && row.configured" :key="`steward-${row.id}`" :repo-id="row.id" />
      </section>

      <section v-if="live" class="mt-6 space-y-3" aria-labelledby="advanced-heading">
        <h2 id="advanced-heading" class="text-lg font-semibold text-white">Advanced</h2>
        <RepositorySettingsSection title="Dependency access" :summary="row.mirror_denied_repositories?.length ? `${row.mirror_denied_repositories.length} blocked` : 'No repositories blocked'">
          <RepositoryMirrorPolicyCard :key="`mirror-${row.id}`" :entries="row.mirror_denied_repositories ?? []" :busy="busy" :error="mirrorError" :notice="mirrorNotice" @save="updateMirrorPolicy" />
        </RepositorySettingsSection>
        <RepositorySettingsSection v-if="row.environment" title="Test environment" :summary="row.environment.error ? 'Needs attention' : environmentSummary">
          <RepositoryEnvironmentCard :key="row.id" :environment="row.environment" :busy="busy" :action="environmentAction" :error="environmentError" :notice="environmentNotice" :access-ready="row.access_ready"
            @scan="updateEnvironment('scan')" @confirm="updateEnvironment('confirm')" @automatic="updateEnvironment('automatic')" />
        </RepositorySettingsSection>
        <RepositorySettingsSection v-if="deployment" title="Deployment tracking" :summary="deployment.status" @update:open="deploymentOpen = $event">
          <DeploymentCard :card="deployment" :busy="busy" :error="deployError"
            @confirm="domain => decide('confirm', { domain })" @reject="rejecting = true" @reread="decide('reread')" />
        </RepositorySettingsSection>
      </section>

      <div class="mt-6">
        <RepositorySettingsSection title="Diagnostics" :summary="row.access_ready && displayReady ? 'Repository checks ready' : 'Check repository setup'">
          <p class="text-sm text-muted">GitHub access: {{ row.access_ready ? 'Connected' : 'Needs attention' }}</p>
          <div class="mt-3 flex flex-wrap items-center gap-3">
            <UButton color="neutral" variant="outline" :disabled="busy || !row.access_ready" :loading="checking" @click="check">Refresh repository</UButton>
            <NuxtLink to="/settings/github" class="text-sm text-sky-200 underline">Manage GitHub access</NuxtLink>
            <span id="repo-checked-at" class="text-xs text-muted">{{ checkedAtText(report, localTime) }}</span>
          </div>
          <RepositoryRulesPanel id-prefix="repo-" :diagnostics-only="true" :row="row" :live="live" :report="report" :display-ready="displayReady" :hint="hint" />
        </RepositorySettingsSection>
      </div>
    </template>
    <RepositorySetupWizard v-if="wizard" :initial-repo-id="repoId" @close="closed" @enabled="closed" />
    <ConfirmDialog :open="disabling" :title="`Disable automation for ${row?.name}?`" text="Forge stops picking up work in this repository. Existing pull requests, branches and history are kept."
      confirm-label="Disable automation" :require-reason="false" :pending="busy" :error="error" @confirm="confirmDisable" @close="disabling = false" />
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
