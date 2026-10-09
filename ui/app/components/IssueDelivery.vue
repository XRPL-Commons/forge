<script setup lang="ts">
import { jobSummary, type JobView } from '~/utils/factory'

const props = defineProps<{ issueKey: string }>()
const open = ref(false)
const cursors = ref<string[]>([''])
const cursor = computed(() => cursors.value.at(-1) ?? '')
const api = useForgeApi()
interface DeliveryView {
  unit_id: string; version: number; state: string; builder_id: string | null
  repo: string; candidate_pr: number | null; charged_failures: number; failure_limit: number
  members: Array<{ id: string; issue_key: string; number: number; title: string; state: string; output_commit: string | null }>
  jobs: JobView[]; next_cursor: string | null
}
const resource = useResource<DeliveryView>(
  () => `/api/delivery?issue=${encodeURIComponent(props.issueKey)}&after=${encodeURIComponent(cursor.value)}`,
  path => api.get(path), { allowed: () => open.value, server: false, maxAge: 5000, interval: 5000 },
)
onMounted(resource.start)
watch(() => props.issueKey, () => { cursors.value = [''] })
const delivery = resource.data
function next() { if (delivery.value?.next_cursor) cursors.value.push(delivery.value.next_cursor) }
function label(job: JobView) {
  const stage = { planning: 'Builder planning', integration: 'Builder integration', child: 'Child implementation' }[job.builder_stage ?? 'planning']
  return job.builder_stage ? `${stage} · ${jobSummary(job).split(' · ').slice(1).join(' · ')}` : jobSummary(job)
}
</script>
<template>
  <IssueSection id="issue-delivery" v-model:open="open" title="Combined delivery">
    <p v-if="resource.error.value" role="status" class="text-sm text-amber-200">{{ resource.error.value }}</p>
    <div v-if="delivery" class="space-y-3 text-sm">
      <p>One builder owns this delivery. Children implement issue scopes; the builder integrates one pull request for review and audit.</p>
      <p class="text-muted">{{ delivery.charged_failures }} / {{ delivery.failure_limit }} shared failures
        <a v-if="delivery.candidate_pr" :href="`https://github.com/${delivery.repo}/pull/${delivery.candidate_pr}`" target="_blank" rel="noopener noreferrer" class="ml-2 text-sky-200 underline">PR #{{ delivery.candidate_pr }} ↗</a>
      </p>
      <ul aria-label="Delivery members" class="space-y-1">
        <li v-for="member in delivery.members" :key="member.id">
          <NuxtLink :to="`/issues/${encodeURIComponent(member.issue_key)}`" class="text-sky-200 hover:underline">#{{ member.number }} {{ member.title }}</NuxtLink>
          <span class="ml-2 text-xs text-muted">{{ member.output_commit ? 'Output accepted' : member.state }}</span>
        </li>
      </ul>
      <ul aria-label="Delivery executions" class="space-y-2">
        <li v-for="job in delivery.jobs" :key="job.id" :class="job.parent_job_id ? 'ml-4 border-l border-line pl-3' : ''">
          <NuxtLink :to="`/issues/${encodeURIComponent(job.issue_key ?? issueKey)}?job=${encodeURIComponent(job.id)}`" class="hover:underline">{{ label(job) }}</NuxtLink>
          <span v-if="job.builder_stage === 'child'" class="ml-2 text-xs text-muted">#{{ job.issue_number }} · {{ job.delegation_key }}</span>
          <span v-else-if="job.members && job.members.length > 1" class="ml-2 text-xs text-muted">{{ job.members.length }} issues</span>
        </li>
      </ul>
      <p v-if="!delivery.jobs.length" class="text-muted">The builder has not started yet.</p>
      <nav v-if="cursors.length > 1 || delivery.next_cursor" aria-label="Delivery history" class="flex gap-3">
        <UButton v-if="cursors.length > 1" size="xs" color="neutral" variant="ghost" @click="cursors.pop()">Previous runs</UButton>
        <UButton v-if="delivery.next_cursor" size="xs" color="neutral" variant="ghost" @click="next">More runs</UButton>
      </nav>
    </div>
    <p v-else-if="!resource.error.value" class="text-sm text-muted">Loading delivery…</p>
  </IssueSection>
</template>
