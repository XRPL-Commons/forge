<script setup lang="ts">
import { repositoryRows, actionMessage, type SettingsView, type OnboardingSummary, type RepositoryRowModel } from '~/utils/repositories'
useHead({ title: 'Repositories' })
const api = useForgeApi()
const route = useRoute()
const busy = ref(false)
const checkingRepositories = ref(false)
const query = ref('')
const notice = ref('')
const noticeError = ref(false)
const wizard = ref(route.query.add === '1')
const wizardRepo = ref<number | null>(null)
watch(() => route.query.add, value => { if (value === '1') { wizardRepo.value = null; wizard.value = true } })
const poll = useResource('settings-repositories', async () => {
  const [settings, onboarding] = await Promise.all([api.get<SettingsView>('/api/settings?summary=1'), api.get<OnboardingSummary>('/api/onboarding')])
  return { settings, onboarding }
}, { interval: useRuntimeConfig().public.pollInterval, allowed: () => !busy.value && !wizard.value })
onMounted(() => poll.start())
const settings = computed(() => poll.data.value?.settings)
const settingUp = computed(() => !!poll.data.value && poll.data.value.onboarding.phase !== 'active')
const rows = computed(() => repositoryRows(settings.value?.repositories ?? [], settingUp.value))
const visibleRows = computed(() => rows.value.filter(row => row.name.toLowerCase().includes(query.value.trim().toLowerCase())))
const enabledCount = computed(() => settings.value?.repositories.filter(row => row.enabled).length ?? 0)
const repositoryCount = computed(() => `${enabledCount.value} enabled · ${rows.value.length} discovered or configured`)
const repositoriesChecking = computed(() => checkingRepositories.value || settings.value?.repositories.some(row => row.protection_status === 'checking'))
function say(text: string, error = false) { notice.value = text; noticeError.value = error }
async function closeWizard() { wizard.value = false; if (route.query.add) await navigateTo('/settings/repositories', { replace: true }); await poll.refresh() }
async function enabled(id: number) { wizard.value = false; await navigateTo(`/settings/repositories/${id}`) }
async function refreshRepositories() {
  const current = settings.value
  if (!current || busy.value) return
  busy.value = true
  checkingRepositories.value = true
  say('')
  try {
    await api.act('/api/repositories/check', { repo_id: null, revision: current.revision })
    busy.value = false
    await poll.refresh()
  } catch (error: unknown) {
    say(actionMessage(error, 'Could not start the check.'), true)
  } finally {
    busy.value = false
    checkingRepositories.value = false
  }
}

const disableTarget = ref<RepositoryRowModel | null>(null)

function onToggle(row: RepositoryRowModel) {
  if (row.action === 'continue') return navigateTo('/onboarding')
  if (row.action === 'setup') { wizardRepo.value = row.id; wizard.value = true; return }
  disableTarget.value = row
}

async function confirmDisable() {
  const target = disableTarget.value
  disableTarget.value = null
  if (target) await disable(target.id)
}

async function disable(id: number) {
  const current = settings.value
  if (!current || busy.value) return
  const repository = current.repositories.find(row => row.id === id)
  if (!repository) return
  busy.value = true
  say('')
  try {
    await api.act('/api/repositories/disable', { repo_id: id, revision: current.revision })
    say('Settings saved.')
    busy.value = false
    await poll.refresh()
  } catch (error: unknown) {
    say(actionMessage(error, 'Settings unavailable'), true)
  } finally {
    busy.value = false
  }
}
</script>
<template>
  <div>
    <p class="text-xs uppercase tracking-wide text-muted">Settings</p>
    <h1 class="text-3xl font-semibold text-white">Repositories</h1>
    <p class="mt-1 text-sm text-muted">Choose where Forge works and manage each repository’s settings.</p>
    <p v-if="notice || poll.error.value" :role="noticeError || poll.error.value ? 'alert' : 'status'" class="mt-3 text-sm">{{ notice || poll.error.value }}</p>
    <section aria-labelledby="repos-title" class="mt-4 rounded-xl border border-line bg-bg p-4">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <div class="flex flex-wrap items-center gap-3">
          <h2 id="repos-title" class="text-sm font-semibold text-white">Repositories</h2>
          <span id="repo-count" class="text-xs text-muted">{{ repositoryCount }}</span>
        </div>
        <div class="flex items-center gap-2">
          <UButton @click="wizardRepo = null; wizard = true">Add repository</UButton>
          <UButton
            v-if="!settingUp"
            color="neutral"
            variant="ghost"
            aria-label="Refresh repositories"
            title="Refresh repositories"
            :disabled="busy || !enabledCount"
            :aria-busy="repositoriesChecking ? 'true' : undefined"
            icon="i-lucide-refresh-cw"
            :ui="{ leadingIcon: repositoriesChecking ? 'animate-spin' : '' }"
            @click="refreshRepositories"
          />
        </div>
      </div>
      <p class="mt-1 text-xs text-muted">Choose Enable to check a repository and finish its setup. Automation starts only after you confirm.</p>

      <label for="repo-search" class="mt-3 block text-xs text-muted">Find a repository</label>
      <UInput id="repo-search" v-model="query" type="search" placeholder="Repository name" autocomplete="off" class="mt-1 w-full max-w-sm" />

      <RepositoryTable v-if="rows.length" class="mt-3" :rows="visibleRows" :busy="busy" @toggle="onToggle" />
      <p v-else-if="settings" id="repo-empty" class="mt-3 text-xs text-muted">Connect the Forge Apps through setup, then refresh access to discover repositories.</p>

      <p class="mt-3 text-xs text-muted">Forge checks existing issues and pull requests before starting work. If access is lost, new work pauses and issue history is kept.</p>
    </section>
  </div>
  <RepositorySetupWizard v-if="wizard" :initial-repo-id="wizardRepo" @close="closeWizard" @enabled="enabled" />
  <ConfirmDialog
    :open="disableTarget !== null"
    :title="disableTarget ? `Disable automation for ${disableTarget.name}?` : ''"
    text="Forge stops picking up issues in this repository. Existing pull requests, branches and issue history are kept."
    confirm-label="Disable"
    :require-reason="false"
    @confirm="confirmDisable"
    @close="disableTarget = null"
  />
</template>
