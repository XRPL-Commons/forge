<script setup lang="ts">
import { requestId } from '~/utils/requestId'
import { ACTION_LABELS, type IssueAction } from '~/utils/issues'
interface Message { id: string; sequence: number; role: 'user' | 'assistant'; text: string; proposal?: { action: IssueAction; reason: string } | null; receipt?: string }
interface Conversation { messages: Message[]; before: number | null; status: string; provider?: string; reason: string | null; partial: string; origin: { role: string; provider: string; link: string } | null }
const props = defineProps<{ issueKey: string }>()
const api = useForgeApi()
const poll = useResource<Conversation>(() => `/api/issues/discussion?issue=${encodeURIComponent(props.issueKey)}`, path => api.get<Conversation>(path), { interval: 2500 })
const open = ref(false)
const question = ref('')
const error = ref('')
const sending = ref(false)
const applying = ref<string | null>(null)
const older = ref<Message[]>([])
const olderBefore = ref<number | null | undefined>(undefined)
const before = computed(() => olderBefore.value === undefined ? poll.data.value?.before : olderBefore.value)
const messages = computed(() => [...new Map([...older.value, ...(poll.data.value?.messages ?? [])].map(m => [m.id, m])).values()])
const busy = computed(() => ['queued', 'running', 'cancelling'].includes(poll.data.value?.status ?? ''))
// Retain the identity on transport failure: retry cannot submit the same turn twice.
let draftId: string | null = null
let draftText = ''
watch(open, value => { if (value) poll.start(); else poll.stop() })
async function send() {
  if (!question.value.trim() || sending.value || busy.value) return
  if (draftText !== question.value.trim()) { draftText = question.value.trim(); draftId = requestId() }
  sending.value = true; error.value = ''
  try {
    await api.act('/api/issues/discussion', { action: 'send', key: props.issueKey, message: draftText, request_id: draftId })
    question.value = ''; draftId = null; draftText = ''
    await poll.refresh()
  } catch (e) { error.value = e instanceof Error ? e.message : 'Could not send. Retry uses the same message identity.' }
  finally { sending.value = false }
}
async function cancel() {
  error.value = ''
  try { await api.act('/api/issues/discussion', { action: 'cancel', key: props.issueKey }); await poll.refresh() }
  catch (e) { error.value = e instanceof Error ? e.message : 'Could not cancel the discussion' }
}
async function apply(message: Message) {
  if (applying.value) return
  applying.value = message.id; error.value = ''
  try { await api.act('/api/issues/discussion', { action: 'apply', key: props.issueKey, message_id: message.id }); await poll.refresh() }
  catch (e) { error.value = e instanceof Error ? e.message : 'Could not apply this decision' }
  finally { applying.value = null }
}
async function loadOlder() {
  if (!before.value) return
  try {
    const page = await api.get<Conversation>(`/api/issues/discussion?issue=${encodeURIComponent(props.issueKey)}&before=${before.value}`)
    older.value = [...page.messages, ...older.value]; olderBefore.value = page.before
  } catch (e) { error.value = e instanceof Error ? e.message : 'Could not load earlier messages' }
}
</script>
<template>
  <section class="mt-4 rounded-xl border border-line bg-panel p-3">
    <UButton color="neutral" variant="outline" class="min-h-11" :aria-expanded="open" @click="open = !open">Discuss blocker</UButton>
    <div v-if="open" class="mt-3 flex min-w-0 flex-col gap-3">
      <p class="text-sm text-muted">Discuss why this issue is blocked and what to try next. Applying a proposal submits a decision for Forge to check.</p>
      <p class="text-xs text-muted">New conversation using saved issue evidence.<span v-if="poll.data.value?.provider"> Replying with {{ poll.data.value.provider }}.</span><template v-if="poll.data.value?.origin"> Latest context: {{ poll.data.value.origin.provider }} · {{ poll.data.value.origin.role }}. <NuxtLink :to="poll.data.value.origin.link" class="underline">View attempt</NuxtLink></template></p>
      <UButton v-if="before" color="neutral" variant="ghost" class="self-start min-h-11" @click="loadOlder">Earlier messages</UButton>
      <ol class="flex flex-col gap-3" aria-label="Issue conversation">
        <li v-for="message in messages" :key="message.id" class="min-w-0 rounded-lg border border-line p-3">
          <p class="text-xs font-semibold text-muted">{{ message.role === 'user' ? 'You' : 'Forge agent' }}</p>
          <Suspense v-if="message.role === 'assistant'"><ConversationMessage :id="message.id" :text="message.text" /><template #fallback><p class="mt-1 whitespace-pre-wrap break-words text-sm">{{ message.text }}</p></template></Suspense>
          <p v-else class="mt-1 whitespace-pre-wrap break-words text-sm text-white">{{ message.text }}</p>
          <div v-if="message.proposal" class="mt-3 rounded-lg border border-line p-3">
            <p class="text-sm font-semibold">Proposed decision: {{ ACTION_LABELS[message.proposal.action] }}</p>
            <p class="mt-1 whitespace-pre-wrap break-words text-sm text-muted">{{ message.proposal.reason }}</p>
            <p v-if="message.receipt" role="status" class="mt-2 text-sm text-mint-200">Decision submitted. The issue status above shows whether Forge accepted it.</p>
            <UButton v-else class="mt-2 min-h-11" :disabled="!!applying || busy" @click="apply(message)">{{ applying === message.id ? 'Submitting…' : 'Apply this decision' }}</UButton>
          </div>
        </li>
      </ol>
      <p v-if="poll.data.value?.partial && busy" class="whitespace-pre-wrap break-words text-sm text-muted">{{ poll.data.value.partial }}</p>
      <p role="status" class="text-sm text-muted">{{ sending ? 'Sending…' : poll.data.value?.reason }}</p>
      <p v-if="error || poll.error.value" role="alert" class="text-sm text-amber-200">{{ error || poll.error.value }}</p>
      <label class="text-sm" :for="`question-${issueKey}`">Ask about this issue</label>
      <textarea :id="`question-${issueKey}`" v-model="question" rows="3" maxlength="4000" class="w-full rounded-lg border border-line bg-bg p-3 text-base text-white" :disabled="sending || busy" placeholder="Why did the last attempt fail? What would unblock it?" />
      <div class="flex flex-wrap gap-2">
        <UButton class="min-h-11" :disabled="sending || busy || !question.trim()" @click="send">Send</UButton>
        <UButton v-if="busy" color="neutral" variant="outline" class="min-h-11" @click="cancel">Cancel reply</UButton>
      </div>
    </div>
  </section>
</template>
