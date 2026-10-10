<script setup lang="ts">
import { jobSummary, type JobView } from '~/utils/factory'
import { taskStatus, taskPendingReason, type DeliveryView } from '~/utils/delivery'
import { ForgeApiError } from '~/composables/useForgeApi'

const props = defineProps<{ issueKey: string }>()
const open = ref(false)
const cursors = ref<string[]>([''])
const cursor = computed(() => cursors.value.at(-1) ?? '')
const taskCursors = ref<string[]>([''])
const taskCursor = computed(() => taskCursors.value.at(-1) ?? '')
const api = useForgeApi()
const cache = useResources()
const path = computed(() => `/api/delivery?issue=${encodeURIComponent(props.issueKey)}&after=${encodeURIComponent(cursor.value)}&task_after=${encodeURIComponent(taskCursor.value)}`)
const resource = useResource<DeliveryView>(
  path,
  async requested => {
    try { return await api.get<DeliveryView>(requested) }
    catch (error) {
      if (error instanceof ForgeApiError && error.status === 409 && requested === path.value && taskCursor.value) {
        const issuePrefix = `/api/delivery?issue=${encodeURIComponent(props.issueKey)}&`
        taskCursors.value = ['']
        cache.invalidate(key => key.startsWith(issuePrefix), true)
      }
      throw error
    }
  }, { allowed: () => open.value, server: false, maxAge: 5000, interval: 5000 },
)
onMounted(resource.start)
watch(() => props.issueKey, () => { cursors.value = ['']; taskCursors.value = [''] }, { flush: 'sync' })
const delivery = resource.data
function next() { if (delivery.value?.next_cursor) cursors.value.push(delivery.value.next_cursor) }
function nextTasks() { if (delivery.value?.task_next_cursor) taskCursors.value.push(delivery.value.task_next_cursor) }
function taskName(id: string) { return delivery.value?.tasks.find(task => task.id === id)?.key ?? 'Task on another page' }
function memberLabel(id: string) {
  const member = delivery.value?.members.find(member => member.id === id)
  return member ? `#${member.number}` : 'Previous issue scope'
}
function label(job: JobView) {
  const stage = { planning: 'Builder planning', integration: 'Builder integration', child: 'Child implementation',
    task: job.task_executor === 'owner' ? 'Builder implementation' : 'Child implementation' }[job.builder_stage ?? 'planning']
  return job.builder_stage ? `${stage} · ${jobSummary(job).split(' · ').slice(1).join(' · ')}` : jobSummary(job)
}
</script>
<template>
  <IssueSection id="issue-delivery" v-model:open="open" title="Combined delivery">
    <p v-if="resource.error.value" role="status" class="text-sm text-amber-200">{{ resource.error.value }}</p>
    <div v-if="delivery" class="space-y-3 text-sm">
      <p>One builder owns this delivery and can share tasks with children. It integrates the work into one pull request for review and audit.</p>
      <p class="text-muted">{{ delivery.charged_failures }} / {{ delivery.failure_limit }} shared failures
        <a v-if="delivery.candidate_pr" :href="`https://github.com/${delivery.repo}/pull/${delivery.candidate_pr}`" target="_blank" rel="noopener noreferrer" class="ml-2 text-sky-200 underline">PR #{{ delivery.candidate_pr }} ↗</a>
      </p>
      <ul aria-label="Delivery members" class="space-y-3">
        <li v-for="member in delivery.members" :key="member.id">
          <NuxtLink :to="`/issues/${encodeURIComponent(member.issue_key)}`" class="text-sky-200 hover:underline">#{{ member.number }} {{ member.title }}</NuxtLink>
          <span class="ml-2 text-xs text-muted">{{ member.operator_status.label }}</span>
          <ul v-if="member.gates.length" :aria-label="`Blockers for issue ${member.number}`" class="ml-4 list-disc text-xs text-muted">
            <li v-for="gate in member.gates" :key="gate.id === null ? `derived:${gate.code}:${gate.subject_key}:${gate.candidate_id}` : `persisted:${gate.id}`">{{ gate.reason }}</li>
          </ul>
          <p v-else class="text-xs text-muted">{{ member.operator_status.reason }}</p>
          <p v-if="member.contributions.plan_id" class="text-xs text-muted">
            {{ member.contributions.outputs.length }} / {{ member.contributions.task_ids.length }} task outputs accepted
            <span v-if="member.contributions.owner_final_pending"> · Builder has remaining integration work</span>
          </p>
          <ul v-if="member.contributions.outputs.length" :aria-label="`Accepted outputs for issue ${member.number}`" class="text-xs text-muted">
            <li v-for="output in member.contributions.outputs" :key="output.output_id">
              <a :href="`https://github.com/${delivery.repo}/commit/${output.commit_oid}`" target="_blank" rel="noopener noreferrer" class="text-sky-200 hover:underline">{{ output.commit_oid.slice(0, 7) }} ↗</a>
            </li>
          </ul>
          <ul v-if="member.dependencies.length" :aria-label="`Prerequisites for issue ${member.number}`" class="text-xs text-muted">
            <li v-for="dependency in member.dependencies" :key="dependency.member_id">
              Prerequisite <NuxtLink :to="`/issues/${encodeURIComponent(dependency.issue_key)}`" class="text-sky-200 hover:underline">#{{ dependency.number }}</NuxtLink>
              · {{ dependency.contributions.ready ? 'Contributions ready' : 'Waiting for contributions' }}
            </li>
          </ul>
        </li>
      </ul>
      <div v-if="delivery.plan" class="space-y-2">
        <h3 class="font-medium">Builder tasks</h3>
        <ul aria-label="Delivery tasks" class="space-y-3">
          <li v-for="task in delivery.tasks" :key="task.id">
            <p>{{ task.key }} · {{ task.executor === 'owner' ? 'Builder' : 'Child' }} · {{ taskStatus[task.status] }}</p>
            <p class="text-xs text-muted">{{ task.scope }}</p>
            <p class="text-xs text-muted">{{ task.members.map(member => memberLabel(member.member_id)).join(', ') }}</p>
            <p v-if="task.pending_reason" class="text-xs text-muted">{{ taskPendingReason[task.pending_reason] }}</p>
            <p v-if="task.dependencies.length" class="text-xs text-muted">Prerequisites: {{ task.dependencies.map(taskName).join(', ') }}</p>
          </li>
        </ul>
        <p v-if="!delivery.tasks.length && taskCursors.length === 1 && delivery.tasks_complete" class="text-xs text-muted">The builder will implement this delivery during integration.</p>
        <p v-if="delivery.plan.owner_final.member_ids.length" class="text-xs text-muted">Final builder work: {{ delivery.plan.owner_final.scope }}</p>
        <nav v-if="taskCursors.length > 1 || delivery.task_next_cursor" aria-label="Task pages" class="flex gap-3">
          <UButton v-if="taskCursors.length > 1" size="xs" color="neutral" variant="ghost" @click="taskCursors.pop()">Previous tasks</UButton>
          <UButton v-if="delivery.task_next_cursor" size="xs" color="neutral" variant="ghost" @click="nextTasks">More tasks</UButton>
        </nav>
      </div>
      <ul aria-label="Delivery executions" class="space-y-2">
        <li v-for="job in delivery.jobs" :key="job.id" :class="job.parent_job_id ? 'ml-4 border-l border-line pl-3' : ''">
          <NuxtLink :to="`/issues/${encodeURIComponent(job.issue_key ?? issueKey)}?job=${encodeURIComponent(job.id)}`" class="hover:underline">{{ label(job) }}</NuxtLink>
          <span v-if="job.builder_stage === 'task'" class="ml-2 text-xs text-muted">{{ job.task_key }} · {{ job.members?.map(member => `#${member.issue_number}`).join(', ') }}</span>
          <span v-else-if="job.builder_stage === 'child'" class="ml-2 text-xs text-muted">#{{ job.issue_number }} · {{ job.delegation_key }}</span>
          <span v-else-if="job.members && job.members.length > 1" class="ml-2 text-xs text-muted">{{ job.members.length }} issues</span>
          <JobResourceUsage v-if="job.resource_usage" :usage="job.resource_usage" />
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
