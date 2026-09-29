<script setup lang="ts">
definePageMeta({ layout: false })
useHead({ title: 'Sign in' })
const route = useRoute()
const { session, refresh } = useOperator()
const unavailable = ref(!session.value)
async function retry() {
  try { await refresh(); unavailable.value = false } catch { unavailable.value = true }
}
</script>
<template>
  <main class="flex min-h-screen items-center justify-center bg-bg px-6 text-white">
    <section class="w-full max-w-md space-y-5 rounded-xl border border-line p-8">
      <h1 class="text-2xl font-semibold">Sign in to Forge</h1>
      <template v-if="unavailable">
        <p role="alert" class="text-muted">The sign-in service is unavailable. Try again in a moment.</p>
        <UButton @click="retry">Try again</UButton>
      </template>
      <template v-else>
        <p class="text-muted">Use the GitHub account your Forge administrator has authorized.</p>
        <p v-if="route.query.error" role="alert">Sign-in did not complete. Your account may not have access, or the sign-in attempt may have expired.</p>
        <UButton to="/auth/github/start" external icon="i-lucide-github">Continue with GitHub</UButton>
      </template>
    </section>
  </main>
</template>
