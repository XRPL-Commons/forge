<script setup lang="ts">
const localTime = useLocalTime()
import { checkedAtText, repoStage, rowState } from '~/utils/repositories'
const props = defineProps<{ initialRepoId?: number | null }>()
const emit = defineEmits<{ close: []; enabled: [number] }>()
const repoId = ref<number | null>(props.initialRepoId ?? null)
const selectedId = ref<number | null>(null)
const step = ref<0 | 1 | 2 | 3>(0)
let closed = false
function close() { closed = true; emit('close') }
let initialized = !props.initialRepoId
const { view, live, row, report, shown, displayReady, ready, prepared, pickerRows, connections, accessDescription, accessNote, syncing, failed, hint, editUrl, downloadHref, downloaded, busy, checking, enabling, error, query, check, enable, refreshAccess, updateGithubProtection } = useRepository(repoId)
watch(row, value => {
  if (value && !initialized) { step.value = Math.min(3, repoStage(value, live.value)) as 1 | 2 | 3; initialized = true }
}, { immediate: true })
const blocked = computed(() => step.value === 1 ? !row.value?.access_ready : step.value === 2 ? !ready.value : false)
const primary = computed(() => step.value === 0 ? 'Continue' : blocked.value ? 'Check again' : step.value < 3 ? 'Continue' : !live.value ? 'Continue to Forge' : row.value?.enabled ? 'Done' : 'Enable automation')
const waiting = computed(() => busy.value || syncing.value || checking.value)
const disabled = computed(() => waiting.value || (step.value === 0 ? selectedId.value === null : !row.value) || (step.value === 3 && (!ready.value || !row.value?.access_ready)))
function back() {
  step.value = Math.max(0, step.value - 1) as 0 | 1 | 2
  if (step.value === 0) { selectedId.value = repoId.value; repoId.value = null }
}
async function next() {
  if (disabled.value) return
  if (step.value === 0) { repoId.value = selectedId.value; step.value = 1; return }
  if (step.value === 1 && blocked.value) { await refreshAccess(); return }
  if (step.value === 2 && blocked.value) { await check(); return }
  if (step.value < 3) { step.value = (step.value + 1) as 2 | 3; return }
  if (!live.value) { await navigateTo(view.value?.continue_url || '/onboarding'); return }
  if (row.value?.enabled) { emit('close'); return }
  await enable()
  if (!closed && row.value?.enabled && repoId.value !== null) emit('enabled', repoId.value)
}
</script>
<template>
  <UModal :open="true" title="Set up a repository" description="Check access and review rules, then enable automation." :ui="{ content: 'max-w-2xl max-h-[90dvh]', body: 'overflow-y-auto', footer: 'shrink-0' }" @update:open="value => !value && close()">
    <template #body>
      <RepositoryStage :stage="step" :activation-available="live" :blocked="blocked" />
      <UAlert v-if="error" id="error" class="mt-3" role="alert" color="error" variant="subtle" :description="error" />
      <section v-if="step === 0" class="mt-4">
        <h2 class="font-semibold text-white">Choose your repository</h2>
        <label for="wizard-repo-search" class="mt-3 block text-sm">Find by name</label>
        <UInput id="wizard-repo-search" v-model="query" type="search" placeholder="owner/repository" class="mt-1 w-full" />
        <p v-if="!view && !error" role="status" class="mt-3 text-sm text-muted">Loading repositories…</p>
        <fieldset class="mt-3">
          <legend class="sr-only">Repository</legend>
          <ul id="repo-options" class="space-y-2">
            <li v-for="candidate in pickerRows" :key="candidate.id">
              <label class="flex cursor-pointer items-center gap-3 rounded-lg border border-line p-3 has-[:checked]:border-mint-200 has-[:disabled]:cursor-default has-[:disabled]:opacity-60">
                <input v-model="selectedId" type="radio" name="repository" :value="candidate.id" :disabled="candidate.enabled && candidate.access_ready && candidate.protection_ready" class="shrink-0 accent-emerald-300" />
                <span class="min-w-0"><span class="block break-all text-sm font-medium">{{ candidate.name }}</span><span class="text-xs text-muted">{{ rowState(candidate).label }}</span></span>
              </label>
            </li>
          </ul>
        </fieldset>
        <p v-if="view && !pickerRows.length" id="no-repositories" class="mt-3 text-sm text-muted">No matching repositories yet.</p>
        <p class="mt-4 text-xs text-muted">Repository missing? <NuxtLink to="/settings/github" class="text-sky-200 underline">Manage GitHub access</NuxtLink>, grant the Apps access, then refresh.</p>
        <UButton class="mt-2" color="neutral" variant="outline" :disabled="!view || syncing" :loading="syncing" @click="refreshAccess">Refresh access</UButton>
        <p v-if="accessNote" role="status" class="mt-2 text-xs text-muted">{{ accessNote }}</p>
      </section>
      <RepositoryAccessPanel v-else-if="step === 1" :connections="connections" :access-description="accessDescription" :access-note="accessNote" />
      <template v-else-if="row && step === 2">
        <RepositoryProtectionPolicy :mode="row.github_protection ?? 'required'" :busy="busy || checking" @change="updateGithubProtection" />
        <RepositoryRulesPanel :row="row" :live="live" :report="report" :display-ready="displayReady" :shown="shown" :failed="failed" :hint="hint" :prepared="prepared" :edit-url="editUrl" :download-href="downloadHref" :downloaded="downloaded" :controller-slug="view?.connections?.controller?.app_slug" instructions @download="downloaded = true" />
        <p id="checked-at" class="mt-3 text-xs text-muted">{{ checkedAtText(report, localTime) }}</p>
      </template>
      <section v-else-if="row && step === 3" class="mt-4">
        <h2 class="break-all text-lg font-semibold">{{ row.name }}</h2>
        <p class="mt-3 text-sm">{{ row.enabled ? 'Automation is enabled for this repository.' : 'Repository checks are ready. Enable automation to let Forge pick up eligible issues in this repository.' }}</p>
        <p v-if="!ready || !row.access_ready" role="alert" class="mt-3 text-sm text-amber-200">Repository readiness changed. Go back to check access and review rules.</p>
      </section>
      <p v-else class="mt-4 text-sm text-muted">Loading repository…</p>
    </template>
    <template #footer>
      <DialogFooter>
        <template #start>
          <UButton color="neutral" variant="ghost" :disabled="step === 0 || busy" @click="back">Back</UButton>
        </template>
        <template #secondary>
          <UButton color="neutral" variant="ghost" @click="close">Cancel</UButton>
        </template>
        <template #primary>
          <UButton :disabled="disabled" :loading="waiting || enabling" @click="next">{{ primary }}</UButton>
        </template>
      </DialogFooter>
    </template>
  </UModal>
</template>
