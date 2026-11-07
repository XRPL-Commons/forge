<script setup lang="ts">
import { signInNotice } from '~/utils/signIn'

definePageMeta({ layout: false })
useHead({ title: 'Sign in' })
const route = useRoute()
const { name, named } = useFactoryIdentity()
const setupPending = useRuntimeConfig().public.forgeSetupPending
const signInUrl = computed(() => '/auth/github/start' + (typeof route.query.return_to === 'string' ? '?return_to=' + encodeURIComponent(route.query.return_to) : ''))
const { session, refresh } = useOperator()
const unavailable = ref(!session.value)
const retrying = ref(false)
async function retry() {
  retrying.value = true
  try { await refresh(); unavailable.value = false } catch { unavailable.value = true } finally { retrying.value = false }
}
const notice = computed(() => signInNotice({ error: route.query.error, returnTo: route.query.return_to, unavailable: unavailable.value, setupPending }))
const mood = computed(() => notice.value?.mood ?? 'waving')
const TONE = {
  coral: 'border-coral/45 bg-coral/10 [&_strong]:text-coral',
  amber: 'border-amber-200/45 bg-amber-200/10 [&_strong]:text-amber-200',
  mint: 'border-mint-300/45 bg-mint-300/10 [&_strong]:text-mint-200',
} as const

// The browser leaves for GitHub; until it does, say so. A back navigation can
// restore this page from the cache, so put the button back then.
const opening = ref(false)
function onPageShow(event: PageTransitionEvent) { if (event.persisted) opening.value = false }
onMounted(() => window.addEventListener('pageshow', onPageShow))
onBeforeUnmount(() => window.removeEventListener('pageshow', onPageShow))
</script>

<template>
  <main class="sign-in flex min-h-screen flex-col items-center justify-center overflow-x-hidden bg-bg px-4 py-8 text-white sm:px-6">
    <div class="w-full max-w-[620px]">
      <div class="overflow-hidden rounded-2xl border border-line bg-[#16191f]">
        <SignInScene :mood="mood" />
      </div>
      <section aria-labelledby="sign-in-title" class="relative mx-auto -mt-2.5 w-full max-w-md space-y-5 rounded-xl border border-line bg-panel p-6 shadow-[0_18px_40px_-20px_rgba(0,0,0,.8)] sm:-mt-3 sm:p-8">
        <div class="flex items-center gap-2.5 text-lg font-bold tracking-tight text-white">
          <span aria-hidden="true" class="grid size-7 shrink-0 -rotate-6 place-items-center rounded-md border border-mint-200 text-mint-200">F</span>
          <span class="min-w-0 break-words [overflow-wrap:anywhere]">Forge<span v-if="named" class="ml-2 text-xs font-normal text-muted">{{ name }}</span></span>
        </div>
        <div class="space-y-2">
          <h1 id="sign-in-title" class="text-2xl font-semibold leading-tight text-balance">{{ setupPending ? 'Your factory is getting ready' : 'Welcome back to the factory floor' }}</h1>
          <p class="text-muted">{{ setupPending ? 'Continue setup in the terminal where you started Forge.' : 'Sign in with the GitHub account your Forge administrator authorized.' }}</p>
        </div>
        <div
          v-if="notice"
          :role="notice.role"
          :data-notice="notice.kind"
          class="rounded-lg border px-4 py-3 text-sm leading-relaxed text-[#edf1ee]"
          :class="TONE[notice.tone]"
        >
          <strong class="block font-semibold">{{ notice.title }}</strong>
          {{ notice.text }}
        </div>
        <UButton v-if="setupPending" color="primary" size="lg" icon="i-lucide-rotate-cw" @click="reloadNuxtApp({ force: true })">Check setup progress</UButton>
        <UButton v-else-if="unavailable" color="primary" size="lg" icon="i-lucide-rotate-cw" :loading="retrying" @click="retry">Try again</UButton>
        <a
          v-else
          :href="signInUrl"
          :aria-busy="opening || undefined"
          class="inline-flex min-h-11 w-full items-center justify-center gap-2.5 rounded-md bg-mint-200 px-4 py-2.5 font-semibold text-[#0d1a12] no-underline transition-colors hover:bg-mint-100 focus-visible:outline-2 focus-visible:outline-offset-3 focus-visible:outline-sky-200 focus-visible:outline-solid"
          @click="opening = true"
        >
          <UIcon v-if="opening" name="i-lucide-loader-circle" class="size-5 animate-spin" aria-hidden="true" />
          <UIcon v-else name="i-lucide-github" class="size-5" aria-hidden="true" />
          {{ opening ? 'Opening GitHub…' : 'Continue with GitHub' }}
        </a>
      </section>
    </div>
  </main>
</template>
