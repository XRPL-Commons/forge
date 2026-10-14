<script setup lang="ts">
const localTime = useLocalTime()
import { checkedAtText } from '~/utils/repositories'
const props = defineProps<{ repoId: number }>()
const repoId = computed(() => props.repoId)
const route = useRoute()
const wizard = ref(route.query.setup === '1')
watch(() => route.query.setup, value => { if (value === '1') wizard.value = true })
const { poll, view, live, row, report, shown, displayReady, ready, prepared, failed, hint, editUrl, downloadHref, downloaded, busy, checking, error, deployment, deployError, environmentError, environmentAction, rejecting, check, decide, rejectDeployment, updateEnvironment, mirrorError, updateMirrorPolicy } = useRepository(repoId, computed(() => !wizard.value))
async function closed() { wizard.value = false; await navigateTo(`/settings/repositories/${props.repoId}`, { replace: true }); await poll.refresh() }
</script>
<template>
  <div>
    <NuxtLink to="/settings/repositories" class="text-sm text-sky-200">← Repositories</NuxtLink>
    <h1 class="mt-3 break-all text-3xl font-semibold text-white">{{ row?.name || 'Repository' }}</h1>
    <UAlert v-if="error" id="repo-error" class="mt-3" role="alert" color="error" variant="subtle" :description="error" />
    <template v-if="row">
      <div class="mt-4 flex flex-wrap items-center gap-3">
        <UButton color="neutral" variant="outline" :to="`/planning/${repoId}`">Plan repository work</UButton>
        <UButton @click="wizard = true">{{ row.enabled && ready ? 'Review setup' : 'Continue setup' }}</UButton>
        <UButton color="neutral" variant="outline" :disabled="busy || !row.access_ready" :loading="checking" @click="check">Refresh repository</UButton>
        <span id="repo-checked-at" class="text-xs text-muted">{{ checkedAtText(report, localTime) }}</span>
      </div>
      <section v-if="row.merge_policy" id="repository-merge-policy" class="mt-5 rounded-xl border border-line bg-panel p-4 text-sm">
        <h2 class="font-semibold text-white">Merge policy</h2>
        <p class="mt-1">{{ row.merge_policy === 'auto' ? 'Automatic after CI, Review and Audit pass.' : 'One counted human approval is required before merge.' }}</p>
        <p class="mt-1 text-muted">Audit or verification can still require a person. Change this policy through the Forge CLI.</p>
        <code class="mt-2 block break-all text-xs">forge repo policy {{ row.name }} auto|human --server FORGE_URL</code>
      </section>
      <RepositoryMirrorPolicyCard v-if="live" :key="`mirror-${row.id}`" :entries="row.mirror_denied_repositories ?? []" :busy="busy" :error="mirrorError" @save="updateMirrorPolicy" />
      <RepositoryRulesPanel id-prefix="repo-" :row="row" :live="live" :report="report" :display-ready="displayReady" :shown="shown" :failed="failed" :hint="hint" :prepared="prepared" :edit-url="editUrl" :download-href="downloadHref" :downloaded="downloaded" :controller-slug="view?.connections?.controller?.app_slug" @download="downloaded = true" />
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
