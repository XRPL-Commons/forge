<script setup lang="ts">
import type { PlanningRow, RepositoryNote } from '~/composables/useRepositoryPlanning'
useHead({ title: 'Repository planning' })
const route = useRoute(), id = computed(() => Number(route.params.id))
if (!Number.isSafeInteger(id.value) || id.value <= 0) await navigateTo('/settings/repositories', { replace: true })
const { resource, view, proposal, busy, error, open, members, history, act, decide } = useRepositoryPlanning(id)
const rules = ref(''), nightly = ref(true), editingRules = ref(false)
const editing = ref<PlanningRow | null>(null), reason = ref(''), labels = ref(''), prerequisites = ref(''), question = ref(''), target = ref('new')
const note = ref<RepositoryNote | null>(null), noteOpen = ref(false), noteContent = ref(''), noteKind = ref('convention'), sourceUrl = ref(''), sourceIdentity = ref('')
const undecided = computed(() => proposal.value?.rows.filter(row => row.status === 'proposed') ?? [])
const complete = computed(() => proposal.value && proposal.value.next === null)
function editRules() { rules.value = view.value?.planning.rules ?? ''; nightly.value = !!view.value?.planning.proposals_enabled; editingRules.value = true }
async function saveRules() { await act({ action: 'rules', expected_revision: view.value?.planning.revision, rules: rules.value, proposals_enabled: nightly.value }); if (!error.value) editingRules.value = false }
function edit(row: PlanningRow) { editing.value = row; reason.value = row.reason; labels.value = (row.parameters.add ?? []).join(', '); prerequisites.value = (row.parameters.prerequisites ?? []).join('\n'); question.value = row.parameters.question ?? ''; target.value = row.parameters.publication_target?.candidate_id ?? 'new' }
async function saveRow() {
  if (!editing.value || !proposal.value) return
  const row = editing.value, parameters = { ...row.parameters }
  if (row.action === 'park' || row.action === 'relabel') parameters.add = labels.value.split(',').map(v => v.trim()).filter(Boolean)
  if (row.action === 'order') parameters.prerequisites = prerequisites.value.split('\n').map(v => v.trim()).filter(Boolean)
  if (row.action === 'question') parameters.question = question.value
  if (row.action === 'group') parameters.publication_target = target.value === 'new' ? { kind: 'new' } : { kind: 'existing', ...parameters.existing_prs.find((p: any) => p.candidate_id === target.value), author: parameters.existing_prs.find((p: any) => p.candidate_id === target.value)?.author, ownership_transfer: true }
  await act({ action: 'edit', proposal_id: proposal.value.id, row_id: row.id, expected_version: proposal.value.version,
    value: { action: row.action, members: row.members.map(m => m.issue_id), reason: reason.value, rule_quote: row.rule_quote, parameters } })
  if (!error.value) editing.value = null
}
function editNote(value: RepositoryNote | null) { note.value = value; noteContent.value = value?.content ?? ''; noteKind.value = value?.kind ?? 'convention'; sourceUrl.value = value?.sources[0]?.url ?? ''; sourceIdentity.value = value?.sources[0]?.identity ?? ''; noteOpen.value = true }
async function saveNote(deleted = false) {
  await act({ action: 'note', note_id: note.value?.note_id ?? crypto.randomUUID(), expected_revision: note.value?.revision ?? 0,
    kind: noteKind.value, content: noteContent.value, sources: [{ url: sourceUrl.value, identity: sourceIdentity.value, kind: note.value?.sources[0]?.kind ?? 'document', description: note.value?.sources[0]?.description ?? 'Operator source' }, ...(note.value?.sources.slice(1) ?? [])], deleted })
  if (!error.value) noteOpen.value = false
}
</script>
<template>
  <div class="space-y-6">
    <NuxtLink :to="`/settings/repositories/${id}`" class="text-sm text-primary">← Repository settings</NuxtLink>
    <header class="flex flex-wrap items-start justify-between gap-4">
      <div><h1 class="text-3xl font-semibold">Plan {{ view?.repository.name ?? 'repository work' }}</h1><p class="mt-2 text-muted">One proposal for the open backlog. Review each decision before it changes GitHub or starts a delivery.</p></div>
      <UButton :loading="busy" :disabled="view?.planning.runs.some(run => ['queued','running','captured'].includes(run.status))" @click="act({ action: 'triage' })">Triage now</UButton>
    </header>
    <UAlert v-if="error || resource.error.value" color="error" :description="error || resource.error.value || ''" />
    <section class="rounded-xl border border-default p-5 space-y-3">
      <div class="flex justify-between gap-3"><h2 class="text-lg font-semibold">Standing rules</h2><UButton variant="ghost" @click="editRules">Edit rules</UButton></div>
      <p class="whitespace-pre-wrap">{{ view?.planning.rules || 'No standing rules yet.' }}</p>
      <p class="text-sm text-muted">{{ view?.planning.proposals_enabled ? 'A new proposal is requested each night.' : 'Nightly proposals are paused.' }} A quoted rule is shown beside each decision it informs.</p>
      <form v-if="editingRules" class="space-y-3" @submit.prevent="saveRules">
        <label class="block">Rules<textarea v-model="rules" rows="5" maxlength="14000" class="block w-full rounded border border-default bg-default p-3" /></label>
        <label class="flex gap-2"><input v-model="nightly" type="checkbox">Request nightly proposals</label>
        <UButton type="submit" :loading="busy">Save rules</UButton>
      </form>
    </section>
    <section class="rounded-xl border border-default p-5 space-y-3">
      <div class="flex justify-between gap-3"><h2 class="text-lg font-semibold">Repository notes</h2><UButton variant="ghost" @click="editNote(null)">Add note</UButton></div>
      <p class="text-sm text-muted">Conventions, decisions and lessons are guidance. Changed issue or candidate sources mark notes for refresh.</p>
      <article v-for="entry in view?.planning.notes" :key="entry.note_id" class="border-t border-default pt-3">
        <p class="text-xs text-muted">{{ entry.kind }} · revision {{ entry.revision }}<span v-if="entry.needs_refresh"> · Needs refresh</span></p>
        <p class="whitespace-pre-wrap">{{ entry.content }}</p>
        <a v-for="source in entry.sources" :key="source.url" :href="source.url" target="_blank" rel="noopener noreferrer" class="mr-3 text-sm text-primary">{{ source.description }}</a>
        <UButton variant="ghost" size="xs" @click="editNote(entry)">Correct or archive</UButton>
      </article>
      <form v-if="noteOpen" class="space-y-3" @submit.prevent="saveNote()">
        <label class="block">Kind<select v-model="noteKind" class="ml-2 rounded border border-default bg-default p-2"><option v-for="kind in ['convention','decision','lesson','question','next_action']" :key="kind">{{ kind }}</option></select></label>
        <label class="block">Note<textarea v-model="noteContent" required maxlength="8000" rows="4" class="block w-full rounded border border-default bg-default p-3" /></label>
        <template><label class="block">Source link<input v-model="sourceUrl" required type="url" placeholder="https://…" class="block w-full rounded border border-default bg-default p-2"></label><label class="block">Source revision or immutable identity<input v-model="sourceIdentity" required class="block w-full rounded border border-default bg-default p-2"></label></template>
        <UButton type="submit" :loading="busy">Save note</UButton><UButton v-if="note" color="neutral" variant="ghost" @click="saveNote(true)">Archive</UButton>
      </form>
    </section>
    <section class="space-y-3">
      <h2 class="text-xl font-semibold">Proposals</h2>
      <div class="flex flex-wrap gap-2"><UButton v-for="item in view?.proposals.items" :key="item.id" color="neutral" variant="outline" @click="open(item.id)">{{ item.status }} · {{ new Date(item.created_at * 1000).toLocaleDateString() }}</UButton></div>
      <UButton v-if="view?.proposals.next" variant="ghost" @click="history">Older proposals</UButton>
      <template v-if="proposal">
        <p>{{ proposal.rationale || (proposal.run?.status === 'failed' ? 'This planning run did not produce a complete proposal. You can request triage again.' : 'The planner is preparing this proposal. Refresh to see its result.') }}</p>
        <UButton color="neutral" variant="outline" @click="open(proposal.id)">Refresh proposal</UButton>
        <div v-if="undecided.length" class="flex flex-wrap items-center gap-3"><UButton :disabled="busy || !complete" @click="decide(undecided.map(row => row.id), [])">Accept all {{ complete ? undecided.length : '' }}</UButton><p class="text-sm text-muted">Accepted issue edits run on GitHub. Approved groups become one delivery with a shared failure budget. Continuing an existing PR explicitly transfers its implementation to the builder, preserving its author. Other PRs stay open unless separately closed.</p></div>
        <article v-for="row in proposal.rows" :key="row.id" class="rounded-xl border border-default p-5 space-y-3">
          <div class="flex flex-wrap justify-between gap-2"><h3 class="font-semibold capitalize">{{ row.action }} · {{ row.member_count }} {{ row.member_count === 1 ? 'issue' : 'issues' }}</h3><span class="text-sm text-muted">{{ row.status }}</span></div>
          <ul><li v-for="member in row.members" :key="member.issue_id"><NuxtLink :to="`/issues/${member.issue_id}`" class="text-primary">#{{ member.github_number }} {{ member.title }}</NuxtLink><span v-if="member.pull_number" class="ml-2 text-sm">Existing PR #{{ member.pull_number }} · {{ member.head_oid?.slice(0, 8) }}</span></li></ul>
          <UButton v-if="row.members_next" size="xs" variant="ghost" @click="members(row)">Show more members</UButton>
          <p>{{ row.reason }}</p><blockquote v-if="row.rule_quote" class="border-l-2 border-primary pl-3 text-sm">Rule: “{{ row.rule_quote }}”</blockquote>
          <p v-if="row.parameters.question">{{ row.parameters.question }}</p>
          <p v-if="row.parameters.add?.length">Add labels: {{ row.parameters.add.join(', ') }}</p><p v-if="row.parameters.remove?.length">Remove labels: {{ row.parameters.remove.join(', ') }}</p>
          <p v-if="row.parameters.prerequisites">Prerequisites: {{ row.parameters.prerequisites.join(', ') || 'None' }}</p>
          <p v-if="row.action === 'group'">Publish {{ row.parameters.publication_target.kind === 'new' ? 'a new PR' : `to existing PR #${row.parameters.publication_target.pull_number}` }}. {{ row.parameters.publication_target.kind === 'existing' ? `You approve transferring implementation of this PR from ${row.parameters.publication_target.author.login} to the delivery builder; its author stays unchanged.` : '' }} One builder coordinates the members, followed by one review and audit.</p>
          <a v-for="source in row.parameters.evidence ?? []" :key="source.url" :href="source.url" target="_blank" rel="noopener noreferrer" class="block text-primary">{{ source.description }}</a>
          <p v-for="effect in row.effects" :key="effect.id" class="text-sm text-muted">GitHub change: {{ effect.status }}</p>
          <div v-if="row.status === 'proposed'" class="flex gap-2"><UButton :disabled="busy" @click="decide([row.id], [])">Accept</UButton><UButton color="neutral" variant="outline" :disabled="busy || !!row.members_next" @click="edit(row)">Edit</UButton><UButton color="neutral" variant="ghost" :disabled="busy" @click="decide([], [row.id])">Reject</UButton></div>
          <form v-if="editing?.id === row.id" class="space-y-3" @submit.prevent="saveRow">
            <label class="block">Reason<textarea v-model="reason" required rows="3" class="block w-full rounded border border-default bg-default p-2" /></label>
            <label v-if="['park','relabel'].includes(row.action)" class="block">Labels to add, separated by commas<input v-model="labels" class="block w-full rounded border border-default bg-default p-2"></label>
            <label v-if="row.action === 'order'" class="block">Prerequisites, one owner/repository#number per line<textarea v-model="prerequisites" rows="3" class="block w-full rounded border border-default bg-default p-2" /></label>
            <label v-if="row.action === 'question'" class="block">Question<textarea v-model="question" class="block w-full rounded border border-default bg-default p-2" /></label>
            <label v-if="row.action === 'group'" class="block">Publication target<select v-model="target" class="ml-2 rounded border border-default bg-default p-2"><option value="new">New PR</option><option v-for="pr in row.parameters.existing_prs" :key="pr.candidate_id" :value="pr.candidate_id">Existing PR #{{ pr.pull_number }}</option></select></label>
            <UButton type="submit" :loading="busy">Save edit</UButton>
          </form>
        </article>
        <UButton v-if="proposal.next !== null" color="neutral" variant="outline" @click="open(proposal.id, proposal.next)">Show more decisions</UButton>
      </template>
    </section>
  </div>
</template>
