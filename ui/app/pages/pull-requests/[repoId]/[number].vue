<script setup lang="ts">
import { currentPullJob, pullRequestStage, pullRequestStatus } from '~/utils/pullRequests'
const { poll, pull, jobs, selectedJob, selected, output, outputOpen, nextHistory, loadingOlder, historyError, loadOlder } = usePullRequest()
useHead({ title: () => pull.value ? `PR #${pull.value.number} · ${pull.value.repo}` : 'Pull request review' })
const currentSelection = computed(() => !!selected.value && !!pull.value && currentPullJob(selected.value, pull.value))
function date(at: number) { return new Date(at * 1000).toISOString().replace('T', ' ').replace('.000Z', ' UTC') }
</script>

<template>
  <div class="mx-auto max-w-5xl">
    <NuxtLink to="/#pull-request-work" class="text-sm text-mint-200 hover:underline">← Activity</NuxtLink>
    <p v-if="poll.error.value" role="alert" class="mt-4 text-amber-200">{{ poll.error.value }} <button class="underline" @click="poll.refresh()">Try again</button></p>
    <p v-if="!pull && !poll.error.value" class="mt-4 text-muted">Loading pull request review…</p>
    <template v-if="pull">
      <header class="mt-5">
        <a :href="pull.url" target="_blank" rel="noopener noreferrer" class="break-all text-sm text-mint-200 hover:underline">{{ pull.repo }} #{{ pull.number }} ↗</a>
        <h1 class="mt-2 break-words text-3xl font-semibold text-white">{{ pull.title }}</h1>
        <p class="mt-2 text-sm text-muted">Independent Forge Review and Audit</p>
      </header>
      <section id="pull-request-status" class="my-6 rounded-xl border border-line bg-panel p-5" aria-labelledby="pull-request-stage">
        <h2 id="pull-request-stage" class="text-lg font-semibold" :class="pull.status === 'blocked' || pull.status === 'deferred' ? 'text-amber-200' : 'text-mint-200'">{{ pullRequestStage(pull.stage_code) }} · {{ pullRequestStatus(pull.status) }}</h2>
        <p v-if="pull.reason" class="mt-2 text-sm">{{ pull.reason }}</p>
        <p v-if="pull.retry_at" class="mt-2 text-sm text-muted">Next automatic retry: <time :datetime="new Date(pull.retry_at * 1000).toISOString()">{{ date(pull.retry_at) }}</time></p>
        <p v-if="pull.status === 'queued' && !pull.active_job_id" class="mt-2 text-sm text-muted">No worker is assigned yet. This pull request remains in the review queue.</p>
        <p v-if="pull.candidate" class="mt-3 break-all text-xs text-muted">Current revision <code>{{ pull.candidate.head.slice(0, 12) }}</code> → {{ pull.candidate.base_ref }} <code>{{ pull.candidate.base.slice(0, 12) }}</code></p>
        <p class="mt-2 text-xs text-muted">Last update <time :datetime="new Date(pull.updated_at * 1000).toISOString()">{{ date(pull.updated_at) }}</time><span v-if="poll.error.value"> · Refresh failed; showing the last known state.</span></p>
      </section>
      <section v-if="jobs.length" aria-labelledby="pull-attempts-title">
        <h2 id="pull-attempts-title" class="mb-2 text-lg font-semibold text-white">Worker attempts</h2>
        <p v-if="selected" id="pull-selected-attempt" class="mb-3 text-sm text-muted">
          {{ currentSelection ? 'Viewing an attempt for the current revision.' : 'Viewing a historical attempt for an earlier revision.' }}
          <span v-if="selected.id === pull.active_job_id">This is the active worker.</span>
          <span v-else>This attempt is not the active worker.</span>
        </p>
        <IssueWorkerOutput v-model:open="outputOpen" :jobs="jobs" :selected-job="selectedJob" :output="output.data.value" :error="output.error.value" :updated-at="output.updatedAt.value" :current-candidate-id="pull.candidate?.id" />
        <p v-if="historyError" role="status" class="mt-2 text-sm text-amber-200">{{ historyError }}</p>
        <UButton v-if="nextHistory" class="mt-3" color="neutral" variant="outline" :loading="loadingOlder" @click="loadOlder">Load earlier attempts</UButton>
      </section>
      <p v-else class="text-sm text-muted">No worker attempts yet. Progress will appear here when Review starts.</p>
    </template>
  </div>
</template>
