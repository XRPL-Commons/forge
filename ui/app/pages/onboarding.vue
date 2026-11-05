<script setup lang="ts">
import { ForgeApiError } from '~/composables/useForgeApi'
import {
  PROTECTION_HELP_LINK, RESETTING_SCREEN, ROLES, ROLE_NAMES, checksPassed, harnessSummary,
  initialStep, nextRole, usesSingleApp, repositoryProblem, resetStep, signature, step3, summaryVerified,
  technicalChecks, type OnboardingView, type Step3View,
} from '~/utils/onboarding'

// Guided setup (contracts §3.8): three steps, one screen at a time, and a
// poll that only runs while Forge itself is doing something.
useHead({ title: 'Setup' })

const ACCOUNT_TYPES = [
  { label: 'Organization', value: 'organization' },
  { label: 'Personal account', value: 'personal' },
]

const api = useForgeApi()
const installation = useInstallation()
const route = useRoute()

const state = ref<OnboardingView | null>(null)
// 0 GitHub · 1 Repository · 2 Start.
const step = ref(0)
const busy = ref(false)
const actionError = ref('')
// Typed by the operator; a poll must never overwrite either of these, which
// is what the `allowed` predicate below is for.
const owner = ref('')
const accountType = ref<'organization' | 'personal'>('organization')
const repoChoice = ref<string | undefined>(undefined)
// Open/closed state lives here, keyed by section, so a re-render cannot
// collapse a panel the operator is reading.
const showChecks = ref(false)
const showHow = ref(false)
const showPassed = ref(false)
let initial = true

async function load() {
  const wasStarting = state.value?.operation?.action === 'start'
  const next = await api.get<OnboardingView>('/api/onboarding', { timeout: 30000 })
  state.value = next
  if (initial) {
    initial = false
    step.value = initialStep(next)
    repoChoice.value = next.selected ? String(next.selected.id) : undefined
  }
  // The factory came up under a start this page asked for.
  if (wasStarting && next.phase === 'active') {
    await installation.poll.refresh()
    await navigateTo(installation.pending.value ? '/installation' : '/')
  }
  return next
}

// Contracts §4: the onboarding poll is 3 s. The e2e harness lowers the
// shared knob; never poll slower than the contract's 3 s.
const interval = Math.min(3000, useRuntimeConfig().public.pollInterval)
const poll = usePoll(load, {
  interval,
  // Contracts §4: no poll lands while the operator is typing an owner name
  // or choosing an account type — only while an operation runs, during a
  // reset, or on the Start step.
  allowed: () => !busy.value
    && (Boolean(state.value?.operation) || state.value?.phase === 'resetting' || step.value === 2),
})

// `service.last_seen` ticks every second; the render key drops it and adds
// the one flag derived from it, so step 3 is rebuilt only when what it says
// actually changes.
const nowTick = ref(Date.now() / 1000)
let ticker: ReturnType<typeof setInterval> | undefined
const renderKey = computed(() => (state.value ? signature(state.value, nowTick.value) : ''))
const start = shallowRef<Step3View | null>(null)
watch(renderKey, () => { start.value = state.value ? step3(state.value, nowTick.value) : null }, { immediate: true })

onMounted(async () => {
  // Contracts §1: POST /api/onboarding {action:'github'} fails with "Choose
  // a GitHub account type" without this cookie, and a client-side route
  // change into this page never reaches the Nitro middleware that sets it.
  await $fetch('/_ui/setup-cookie').catch(() => {})
  ticker = setInterval(() => { if (!document.hidden) nowTick.value = Date.now() / 1000 }, 1000)
  poll.start()
  try {
    await load()
    if ('connection_error' in route.query) {
      actionError.value = 'GitHub connection was not verified. Reconnect from this page.'
    } else if (state.value && state.value.phase === 'setup' && !state.value.selected
      && ROLES.some(role => state.value!.roles?.[role]?.registered) && !state.value.operation) {
      // Installations registered elsewhere are discovered by a refresh.
      await act('refresh')
    }
  } catch (error: unknown) {
    actionError.value = message(error)
  }
})
onBeforeUnmount(() => clearInterval(ticker))

function message(error: unknown): string {
  if (error instanceof ForgeApiError || error instanceof Error) return error.message
  return 'Forge did not accept the action.'
}

function submitManifest(action: string, manifest: unknown) {
  const form = document.createElement('form')
  form.method = 'POST'
  form.action = action
  const field = document.createElement('input')
  field.type = 'hidden'
  field.name = 'manifest'
  field.value = JSON.stringify(manifest)
  form.append(field)
  document.body.append(form)
  form.submit()
}

async function act(action: 'github' | 'refresh' | 'repository' | 'check' | 'start') {
  if (busy.value || !state.value) return
  busy.value = true
  actionError.value = ''
  try {
    if (action === 'github') {
      const view = state.value
      const role = nextRole(view)
      if (!role) {
        step.value = 1
      } else if (view.roles[role].install_url) {
        window.location.assign(view.roles[role].install_url!)
        return
      } else {
        const result = await api.act<{ install_url?: string; action?: string; manifest?: unknown }>('/api/onboarding', {
          action: 'github',
          role: usesSingleApp(view) ? 'app' : role,
          owner: view.owner || owner.value.trim(),
          account_type: view.account_type || accountType.value,
        })
        if (result.install_url) { window.location.assign(result.install_url); return }
        // The App manifest is handed to GitHub by a form POST, not by fetch:
        // GitHub answers with a redirect the browser has to follow.
        submitManifest(result.action!, result.manifest)
        return
      }
    } else if (action === 'refresh') {
      await api.act('/api/onboarding', { action: 'refresh' })
    } else if (action === 'repository') {
      if (!repoChoice.value) throw new Error('Choose a repository to continue')
      await api.act('/api/onboarding', { action: 'repository', repo_id: Number(repoChoice.value) })
      step.value = 2
    } else {
      await api.act('/api/onboarding', { action, revision: state.value.revision })
    }
    // Loaded directly: poll.refresh() is gated by `allowed`, which is false
    // on steps 1 and 2 with nothing running.
    await load()
  } catch (error: unknown) {
    actionError.value = message(error)
  } finally {
    busy.value = false
  }
}

const resetting = computed(() => state.value?.phase === 'resetting')
const phase = computed(() => state.value?.phase)
const solo = computed(() => resetting.value || phase.value === 'active')
const rolesOk = computed(() => Boolean(state.value) && ROLES.every(role => state.value!.roles?.[role]?.status === 'ok'))
const singleApp = computed(() => !!state.value && usesSingleApp(state.value))
const displayedRoles = computed(() => singleApp.value ? [pending.value || 'controller'] as const : ROLES)
const pending = computed(() => (state.value ? nextRole(state.value) : null))
const harnesses = computed(() => state.value?.harnesses ?? [])
const repositories = computed(() => state.value?.repositories ?? [])
const repoItems = computed(() => repositories.value.map(row => ({
  label: row.name, description: 'GitHub access ready', value: String(row.id),
})))
const checking = computed(() => Boolean(state.value?.operation))
const problem = computed(() => {
  if (!state.value) return null
  const needsRepositoryCheck = Boolean(state.value.repository_setup) && !state.value.repository_setup!.protection_ready
  return needsRepositoryCheck ? null : repositoryProblem(state.value)
})
const checkRows = computed(() => (state.value ? technicalChecks(state.value) : []))
const checksSuffix = computed(() => (state.value && checksPassed(state.value) ? ' — coding accounts and workers passed' : ''))

// The repository-check failure is already explained inline on step 3, so it
// is not repeated in the notice (contracts §3.8, "Notice").
const explainedHere = computed(() => step.value === 2 && Boolean(problem.value))
const notice = computed(() => actionError.value
  || (explainedHere.value ? '' : state.value?.error)
  || poll.error.value
  || '')

function activityFor(target: string): string | null {
  return state.value?.activity?.target === target ? state.value.activity.label : null
}
</script>

<template>
  <div class="mx-auto" :class="solo ? 'max-w-[780px]' : 'grid max-w-[1280px] gap-8 md:grid-cols-[210px_minmax(0,1fr)] md:gap-16'">
    <aside v-if="!solo">
      <SetupRail
        :step="step"
        :roles-ok="rolesOk"
        :has-selection="Boolean(state?.selected)"
        @select="index => step = index"
      />
    </aside>

    <div class="min-w-0 max-w-[760px]">
      <p v-if="notice" role="alert" class="mb-4 break-words text-amber-200">{{ notice }}</p>

      <p v-if="!state" class="text-muted">Loading setup…</p>

      <!-- Reset in progress (contracts §3.8). -->
      <template v-else-if="resetting">
        <p class="text-xs font-semibold tracking-[1.4px] text-mint-200 uppercase">{{ RESETTING_SCREEN.eyebrow }}</p>
        <h1 class="mt-2 text-[32px] leading-tight font-semibold tracking-tight">{{ RESETTING_SCREEN.title }}</h1>
        <p class="mt-2 text-muted">{{ RESETTING_SCREEN.intro }}</p>
        <div class="mt-6 flex items-center gap-4 rounded-xl border border-line bg-panel px-5 py-4">
          <span class="size-3.5 rounded-full border border-line border-t-mint-200 motion-safe:animate-spin" aria-hidden="true" />
          <span role="status">{{ resetStep(state.operation) }}</span>
        </div>
        <p class="mt-4 text-[13px] text-muted">{{ RESETTING_SCREEN.note }}</p>
      </template>

      <template v-else-if="phase === 'active' && installation.pending.value">
        <p class="text-xs font-semibold tracking-[1.4px] text-mint-200 uppercase">Installation in progress</p>
        <h1 class="mt-2 text-[32px] leading-tight font-semibold tracking-tight">Factory checks passed</h1>
        <p class="mt-2 text-muted">Finish the first merge test to enable your selected repositories.</p>
        <UButton class="mt-6" to="/installation">View setup progress</UButton>
      </template>

      <template v-else-if="phase === 'active'">
        <p class="text-xs font-semibold tracking-[1.4px] text-mint-200 uppercase">Setup complete</p>
        <h1 class="mt-2 text-[32px] leading-tight font-semibold tracking-tight">Forge is enabled</h1>
        <p class="mt-2 text-muted">Open the factory to see the service status and the next eligible issue.</p>
        <UButton class="mt-6" color="neutral" variant="outline" to="/">Open Activity</UButton>
      </template>

      <!-- Step 1 · GitHub -->
      <template v-else-if="step === 0">
        <p class="text-xs font-semibold tracking-[1.4px] text-mint-200 uppercase">1 · GitHub</p>
        <h1 class="mt-2 text-[32px] leading-tight font-semibold tracking-tight">Connect your GitHub workspace</h1>
        <p class="mt-2 text-muted">Choose which repositories Forge can access on GitHub.</p>

        <div class="mt-6 overflow-hidden rounded-xl border border-line bg-panel">
          <div v-for="role in displayedRoles" :key="role" class="flex items-center gap-4 border-b border-line px-5 py-4 last:border-b-0">
            <span class="grid size-7 shrink-0 place-items-center" :class="state.roles[role].status === 'ok' ? 'text-mint-200' : 'text-muted'">
              {{ state.roles[role].status === 'ok' ? '✓' : '○' }}
            </span>
            <div class="min-w-0 flex-1">
              <h3 class="text-[15px] font-medium">{{ singleApp ? 'Forge' : ROLE_NAMES[role] }}</h3>
              <small class="block text-[13px] text-muted">
                {{ state.roles[role].status === 'ok' ? 'Connected'
                  : state.roles[role].registered ? 'App created · install it on GitHub'
                    : 'Connect on GitHub' }}
              </small>
            </div>
          </div>
        </div>

        <template v-if="!state.owner">
          <label for="owner" class="mt-5 block text-sm">GitHub account or organization</label>
          <UInput id="owner" v-model="owner" class="mt-2 w-full" autocomplete="organization" placeholder="your-organization" />
          <label for="account-type" class="mt-4 block text-sm">Account type</label>
          <USelectMenu
            id="account-type"
            v-model="accountType"
            class="mt-2 w-64"
            :items="ACCOUNT_TYPES"
            value-key="value"
            :search-input="false"
          />
        </template>
        <p v-else class="mt-5 text-[13px] text-muted">GitHub owner: {{ state.owner }}</p>

        <div class="mt-8 flex flex-wrap items-center justify-between gap-4 border-t border-line pt-6">
          <span class="max-w-[355px] text-xs text-muted">
            {{ pending ? 'Approve access on GitHub to continue.' : 'GitHub is connected.' }}
          </span>
          <UButton color="primary" :loading="busy" :disabled="busy" @click="act('github')">
            {{ !pending ? 'Continue' : state.roles[pending].registered ? `Install ${singleApp ? 'Forge' : ROLE_NAMES[pending]}` : `Connect ${singleApp ? 'Forge' : ROLE_NAMES[pending]}` }}
          </UButton>
        </div>
        <UButton class="mt-2" color="neutral" variant="link" :disabled="busy" @click="act('refresh')">Refresh access</UButton>
      </template>

      <!-- Step 2 · Repository -->
      <template v-else-if="step === 1">
        <p class="text-xs font-semibold tracking-[1.4px] text-mint-200 uppercase">2 · Repository</p>
        <h1 class="mt-2 text-[32px] leading-tight font-semibold tracking-tight">Where should Forge work?</h1>
        <p class="mt-2 text-muted">Choose one repository to start with. You can add more later.</p>

        <div class="mt-6 rounded-xl border border-line bg-panel px-5 py-4">
          <URadioGroup v-if="repoItems.length" v-model="repoChoice" :items="repoItems" :ui="{ item: 'py-2' }" />
          <p v-else class="text-muted">No repositories yet. Grant repository access on GitHub, then refresh.</p>
        </div>
        <UButton class="mt-2" color="neutral" variant="link" :disabled="busy" @click="act('refresh')">Refresh access</UButton>

        <div class="mt-8 flex flex-wrap items-center justify-between gap-4 border-t border-line pt-6">
          <span class="max-w-[355px] text-xs text-muted">Selecting a repository does not start work.</span>
          <UButton color="primary" :loading="busy" :disabled="busy || !repoItems.length" @click="act('repository')">Continue</UButton>
        </div>
      </template>

      <!-- Step 3 · Start -->
      <template v-else-if="start">
        <p class="text-xs font-semibold tracking-[1.4px] text-mint-200 uppercase">3 · Start</p>
        <h1 class="mt-2 text-[32px] leading-tight font-semibold tracking-tight">{{ start.title }}</h1>
        <p class="mt-2 text-muted">{{ start.intro }}</p>

        <div class="mt-6 overflow-hidden rounded-xl border border-line bg-panel">
          <div class="flex items-center gap-4 px-4 py-3">
            <div class="min-w-0 flex-1">
              <h3 class="text-[15px] font-medium break-words">{{ state.selected?.name }}</h3>
              <small class="block text-[13px] text-muted">Eligible issues labeled ready · Build → Review → Audit → Merge</small>
              <span v-if="activityFor('github')" role="status" class="mt-1 flex items-center gap-2 text-xs text-muted">
                <span class="size-3.5 rounded-full border border-line border-t-mint-200 motion-safe:animate-spin" aria-hidden="true" />
                {{ activityFor('github') }}
              </span>
            </div>
            <UButton color="neutral" variant="outline" :disabled="checking" @click="step = 1">Change repository</UButton>
          </div>
        </div>

        <!-- Repository verification problem (contracts §3.8, step 3 extras). -->
        <section v-if="problem" aria-label="Repository verification requirements" class="mt-6 overflow-hidden rounded-xl border border-[#74603e] bg-panel">
          <div class="flex items-center gap-4 border-b border-line px-5 py-4">
            <div class="min-w-0 flex-1">
              <h2 class="text-lg">{{ problem.title }}</h2>
              <small class="block text-[13px] text-muted">{{ problem.branch }}</small>
            </div>
            <UButton color="neutral" variant="outline" :to="problem.to">Prepare repository →</UButton>
          </div>
          <div class="px-5 pt-3 pb-5">
            <ul class="list-disc pl-5">
              <li v-for="requirement in problem.missing" :key="requirement.id" class="my-3">
                <strong class="font-medium">{{ requirement.label }}</strong>
                <p class="text-[13px] text-muted">{{ requirement.detail }}</p>
              </li>
            </ul>
            <UCollapsible v-if="problem.passedSummary" v-model:open="showPassed" class="my-3">
              <button type="button" class="text-[13px] text-muted underline-offset-4 hover:underline">{{ problem.passedSummary }}</button>
              <template #content>
                <ul class="mt-2 list-none pl-0 text-[13px] text-muted">
                  <li v-for="requirement in problem.passed" :key="requirement.id" class="my-2">✓ {{ requirement.label }}</li>
                </ul>
              </template>
            </UCollapsible>
            <NuxtLink class="text-sky-200 underline-offset-4 hover:underline" :to="PROTECTION_HELP_LINK.to">{{ PROTECTION_HELP_LINK.label }}</NuxtLink>
          </div>
        </section>

        <!-- Coding accounts summary -->
        <div class="mt-6 overflow-hidden rounded-xl border border-line bg-panel">
          <div class="flex items-center gap-4 border-b border-line px-4 py-3">
            <h3 class="flex-1 text-[15px] font-medium">Coding accounts</h3>
            <NuxtLink class="text-sky-200 underline-offset-4 hover:underline" to="/settings/harnesses">Manage accounts</NuxtLink>
          </div>
          <div v-for="harness in harnesses" :key="harness.id" class="flex items-center gap-4 border-b border-line px-4 py-3 last:border-b-0">
            <span class="grid size-7 shrink-0 place-items-center" :class="summaryVerified(harness) ? 'text-mint-200' : 'text-muted'">
              {{ summaryVerified(harness) ? '✓' : '○' }}
            </span>
            <span class="min-w-0 flex-1 break-words">{{ harness.name }}</span>
            <span class="text-xs" :class="summaryVerified(harness) ? 'text-mint-200' : 'text-muted'">{{ harnessSummary(harness, state.activity) }}</span>
          </div>
        </div>

        <div v-if="activityFor('runner')" class="mt-6 rounded-xl border border-line bg-panel px-4 py-3">
          <span role="status" class="flex items-center gap-2 text-xs text-muted">
            <span class="size-3.5 rounded-full border border-line border-t-mint-200 motion-safe:animate-spin" aria-hidden="true" />
            {{ activityFor('runner') }}
          </span>
        </div>

        <div class="my-6">
          <p v-if="start.status" role="status" class="flex items-center gap-2">
            <span v-if="start.status.busy" class="size-3.5 rounded-full border border-line border-t-mint-200 motion-safe:animate-spin" aria-hidden="true" />
            {{ start.status.label }}
          </p>
          <UButton
            v-else-if="start.primary && start.primary.action === 'link'"
            color="neutral"
            variant="outline"
            :to="start.primary.to"
          >{{ start.primary.label }}</UButton>
          <UButton
            v-else-if="start.primary"
            color="primary"
            :loading="busy"
            :disabled="busy"
            @click="act(start.primary.action as 'check' | 'start')"
          >{{ start.primary.label }}</UButton>
          <p v-if="start.primary?.help" class="mt-3 max-w-[620px] text-[13px] text-muted">{{ start.primary.help }}</p>
          <p v-if="start.secondary" class="mt-3 text-[13px]">
            <NuxtLink class="text-sky-200 underline-offset-4 hover:underline" :to="start.secondary.to">{{ start.secondary.label }}</NuxtLink>
            ·
            <NuxtLink class="text-sky-200 underline-offset-4 hover:underline" :to="PROTECTION_HELP_LINK.to">{{ PROTECTION_HELP_LINK.label }}</NuxtLink>
          </p>
        </div>

        <UCollapsible v-model:open="showChecks" class="my-5">
          <button type="button" class="text-[13px] text-muted underline-offset-4 hover:underline">Technical checks{{ checksSuffix }}</button>
          <template #content>
            <div class="mt-3 overflow-hidden rounded-xl border border-line bg-panel">
              <div v-for="row in checkRows" :key="row.id" class="flex items-center gap-4 border-b border-line px-4 py-3 text-[13px] last:border-b-0">
                <span class="min-w-0 flex-1">{{ row.label }}</span>
                <span class="flex items-center gap-2 text-muted">
                  <span v-if="row.busy" class="size-3.5 rounded-full border border-line border-t-mint-200 motion-safe:animate-spin" aria-hidden="true" />
                  {{ row.value }}
                </span>
              </div>
            </div>
          </template>
        </UCollapsible>

        <UCollapsible v-model:open="showHow" class="my-5">
          <button type="button" class="text-[13px] text-muted underline-offset-4 hover:underline">How Forge will work</button>
          <template #content>
            <p class="mt-3 text-[13px] text-muted">Build → Review → Security audit → Merge. Human approval is required when repository policy or the auditor calls for it.</p>
            <p class="mt-3 text-[13px] text-muted">Three failed attempts stop an issue for a human decision. Each failure is investigated.</p>
          </template>
        </UCollapsible>
      </template>

      <p class="mt-8 text-[13px] text-muted">
        <NuxtLink class="underline-offset-4 hover:underline" to="/setup-help">Getting set up</NuxtLink>
        <span v-if="!resetting"> · <NuxtLink class="underline-offset-4 hover:underline" to="/reset">Reset Forge</NuxtLink></span>
      </p>
    </div>
  </div>
</template>
