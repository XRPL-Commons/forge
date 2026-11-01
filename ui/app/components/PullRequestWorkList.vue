<script setup lang="ts">
import { pullRequestPath, pullRequestStage, pullRequestStatus, type PullRequestWork } from '~/utils/pullRequests'
defineProps<{ items: PullRequestWork[]; more?: string | null; recentTruncated?: boolean }>()
</script>

<template>
  <section id="pull-request-work" aria-labelledby="pull-request-work-title">
    <h2 id="pull-request-work-title" class="text-sm font-semibold text-white">Pull request reviews · {{ items.length }} shown</h2>
    <p class="mt-1 text-xs text-muted">Review and Audit for pull requests opened outside Forge.</p>
    <NuxtLink v-if="more" :to="`/pull-requests?before=${encodeURIComponent(more)}`" class="mt-2 inline-block text-sm text-mint-200 hover:underline">View more active reviews →</NuxtLink>
    <p v-if="recentTruncated" class="mt-2 text-xs text-muted">Only recent completed reviews are shown here. Older reviews remain available through their GitHub check links.</p>
    <div v-if="items.length" class="mt-3 grid gap-2.5 md:grid-cols-2 xl:grid-cols-3">
      <article v-for="pull in items" :key="pull.key" class="min-w-0 rounded-xl border border-line bg-panel p-4" :data-pull-request="pull.key">
        <div class="flex flex-wrap items-center justify-between gap-2 text-xs">
          <a :href="pull.url" target="_blank" rel="noopener noreferrer" class="break-all text-muted hover:underline">{{ pull.repo }} #{{ pull.number }} ↗</a>
          <span :class="pull.status === 'blocked' || pull.status === 'deferred' ? 'text-amber-200' : 'text-mint-200'">{{ pullRequestStage(pull.stage_code) }} · {{ pullRequestStatus(pull.status) }}</span>
        </div>
        <NuxtLink :to="pullRequestPath(pull)" class="mt-2 block break-words font-semibold text-white hover:underline">{{ pull.title }}</NuxtLink>
        <p v-if="pull.reason" class="mt-2 text-sm text-muted">{{ pull.reason }}</p>
        <p v-if="pull.retry_at" class="mt-2 text-xs text-muted">Retry scheduled: <time :datetime="new Date(pull.retry_at * 1000).toISOString()">{{ new Date(pull.retry_at * 1000).toISOString().replace('T', ' ').replace('.000Z', ' UTC') }}</time></p>
        <NuxtLink :to="pullRequestPath(pull)" class="mt-3 inline-block text-xs text-mint-200 hover:underline">View review progress</NuxtLink>
      </article>
    </div>
    <p v-else class="mt-2 text-xs text-muted">No pull requests are being tracked for independent review.</p>
  </section>
</template>
