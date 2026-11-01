<script setup lang="ts">
import type { PullRequestList } from '~/utils/pullRequests'
useHead({ title: 'Pull request reviews' })
const route = useRoute()
const api = useForgeApi()
const before = computed(() => typeof route.query.before === 'string' ? route.query.before : '')
const poll = useResource<PullRequestList>(
  () => '/api/pull-requests' + (before.value ? `?before=${encodeURIComponent(before.value)}` : ''),
  path => api.get(path),
)
onMounted(poll.start)
</script>

<template>
  <div>
    <NuxtLink to="/#pull-request-work" class="text-sm text-mint-200 hover:underline">← Activity</NuxtLink>
    <h1 class="mt-5 text-3xl font-semibold text-white">Active pull request reviews</h1>
    <p class="mt-2 text-sm text-muted">Review work for open pull requests, including queued and paused work.</p>
    <p v-if="poll.error.value" role="alert" class="mt-4 text-amber-200">{{ poll.error.value }} <button class="underline" @click="poll.refresh()">Try again</button></p>
    <p v-else-if="!poll.data.value" class="mt-4 text-muted">Loading pull request reviews…</p>
    <PullRequestWorkList v-if="poll.data.value" class="mt-6" :items="poll.data.value.pull_requests" />
    <nav class="mt-5 flex flex-wrap gap-5" aria-label="Pull request review pages">
      <NuxtLink v-if="before" to="/pull-requests" class="text-sm text-mint-200 hover:underline">First page</NuxtLink>
      <NuxtLink v-if="poll.data.value?.next" :to="`/pull-requests?before=${encodeURIComponent(poll.data.value.next)}`" class="text-sm text-mint-200 hover:underline">Next page →</NuxtLink>
    </nav>
  </div>
</template>
