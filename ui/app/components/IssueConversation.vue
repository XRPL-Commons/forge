<script setup lang="ts">
import { requestId } from '~/utils/requestId'
import { ACTION_LABELS, type IssueAction } from '~/utils/issues'
interface Message { id: string; sequence: number; role: 'user' | 'assistant'; text: string; proposal?: { action: IssueAction; reason: string } | null; receipt?: string }
interface Conversation { messages: Message[]; before: number | null; status: string; provider?: string; reason: string | null; partial: string; origin: { role: string; provider: string; link: string } | null }
interface ChatEntry { id: string; role: 'user' | 'assistant'; parts: { type: 'text'; text: string }[] }
// Opened from the issue's action bar ("Discuss blocker"); nothing renders,
// and nothing polls, until it is.
// `actions` are the decisions the panel offers right now and `primary` the
// one it leads with: the agent's proposal and the chips under each reply
// hand the choice back to the panel (`act`), which runs the very same
// confirm dialog as its own buttons.
const props = withDefaults(defineProps<{ issueKey: string; actions?: IssueAction[]; primary?: IssueAction | null; disabled?: boolean }>(), { actions: () => [], primary: null, disabled: false })
const emit = defineEmits<{ act: [action: IssueAction, reason: string] }>()
const open = defineModel<boolean>('open', { default: false })
const api = useForgeApi()
const poll = useResource<Conversation>(() => `/api/issues/discussion?issue=${encodeURIComponent(props.issueKey)}`, path => api.get<Conversation>(path), { interval: 2500 })
const question = ref('')
const error = ref('')
const sending = ref(false)
const sendFailed = ref(false)
const older = ref<Message[]>([])
const olderBefore = ref<number | null | undefined>(undefined)
const before = computed(() => olderBefore.value === undefined ? poll.data.value?.before : olderBefore.value)
const messages = computed(() => [...new Map([...older.value, ...(poll.data.value?.messages ?? [])].map(m => [m.id, m])).values()])
const byId = computed(() => new Map(messages.value.map(m => [m.id, m])))
const busy = computed(() => ['queued', 'running', 'cancelling'].includes(poll.data.value?.status ?? ''))
const partial = computed(() => busy.value ? poll.data.value?.partial ?? '' : '')
// The thread in Nuxt UI's chat message shape. While the agent works, a
// trailing assistant entry carries the streamed text or, with no parts yet,
// makes UChatMessages show its own typing indicator.
const PENDING_ID = 'forge-pending-reply'
const chatMessages = computed<ChatEntry[]>(() => {
  const thread: ChatEntry[] = messages.value.map(m => ({ id: m.id, role: m.role, parts: [{ type: 'text', text: m.text }] }))
  if (busy.value) thread.push({ id: PENDING_ID, role: 'assistant', parts: partial.value ? [{ type: 'text', text: partial.value }] : [] })
  return thread
})
// Drives UChatPromptSubmit: send while ready, stop (cancel the reply) while
// the agent works, retry with the same message identity after a failed send.
const promptStatus = computed(() => {
  if (busy.value) return partial.value ? 'streaming' : 'submitted'
  if (sendFailed.value && question.value.trim()) return 'error'
  return 'ready'
})
const submitLabel = computed(() => ({ ready: 'Send', submitted: 'Stop reply', streaming: 'Stop reply', error: 'Retry send' })[promptStatus.value])
// Retain the identity on transport failure: retry cannot submit the same turn twice.
let draftId: string | null = null
let draftText = ''
watch(open, value => { if (value) poll.start(); else poll.stop() })

// "Discuss blocker" opens the conversation where the operator is: it scrolls
// into view (a jump with reduced motion) and the caret waits in the message
// box. Already open, it only scrolls and focuses.
const root = ref<HTMLElement | null>(null)
async function reveal() {
  open.value = true
  await nextTick()
  const box = root.value?.querySelector<HTMLTextAreaElement>('textarea')
  if (!root.value || !box) return
  const still = window.matchMedia('(prefers-reduced-motion: reduce)').matches
  box.scrollIntoView({ behavior: still ? 'auto' : 'smooth', block: 'center' })
  if (!box.disabled) {
    box.focus({ preventScroll: true })
    const end = box.value.length
    box.setSelectionRange(end, end)
  }
}
defineExpose({ reveal })
async function send() {
  if (!question.value.trim() || sending.value || busy.value) return
  if (draftText !== question.value.trim()) { draftText = question.value.trim(); draftId = requestId() }
  sending.value = true; error.value = ''
  try {
    await api.act('/api/issues/discussion', { action: 'send', key: props.issueKey, message: draftText, request_id: draftId })
    question.value = ''; draftId = null; draftText = ''; sendFailed.value = false
    await poll.refresh()
  } catch (e) { sendFailed.value = true; error.value = e instanceof Error ? e.message : 'Could not send. Retry uses the same message identity.' }
  finally { sending.value = false }
}
async function cancel() {
  if (poll.data.value?.status === 'cancelling') return
  error.value = ''
  try { await api.act('/api/issues/discussion', { action: 'cancel', key: props.issueKey }); await poll.refresh() }
  catch (e) { error.value = e instanceof Error ? e.message : 'Could not cancel the discussion' }
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
  <section v-if="open" id="issue-discussion" ref="root" class="mt-4 rounded-xl border border-line bg-panel p-3">
    <div class="flex min-w-0 flex-col gap-3">
      <p class="text-sm text-muted">Discuss why this issue is blocked and what to try next. Applying a proposal submits a decision for Forge to check.</p>
      <p class="text-xs text-muted">New conversation using saved issue evidence.<span v-if="poll.data.value?.provider"> Replying with {{ poll.data.value.provider }}.</span><template v-if="poll.data.value?.origin"> Latest context: {{ poll.data.value.origin.provider }} · {{ poll.data.value.origin.role }}. <NuxtLink :to="poll.data.value.origin.link" class="underline">View attempt</NuxtLink></template></p>
      <UButton v-if="before" color="neutral" variant="ghost" class="self-start min-h-11" @click="loadOlder">Earlier messages</UButton>
      <!-- The page itself scrolls, so the chat's own scroll-to-bottom and
           jump button (meant for a full-height chat pane) stay off. -->
      <UChatMessages
        v-if="chatMessages.length"
        role="log"
        aria-label="Issue conversation"
        :messages="chatMessages"
        :status="busy ? 'streaming' : 'ready'"
        :should-scroll-to-bottom="false"
        :auto-scroll="false"
        :user="{ side: 'right', variant: 'outline' }"
        :assistant="{ side: 'left', variant: 'naked' }"
        compact
        :ui="{ root: 'min-w-0 gap-3 px-0' }"
      >
        <template #content="{ message }">
          <p class="text-xs font-semibold text-muted">{{ message.role === 'user' ? 'You' : 'Forge agent' }}</p>
          <p v-if="message.id === PENDING_ID" class="mt-1 whitespace-pre-wrap break-words text-sm text-muted">{{ partial }}</p>
          <template v-else-if="byId.get(message.id)">
            <Suspense v-if="message.role === 'assistant'"><ConversationMessage :id="message.id" :text="byId.get(message.id)!.text" /><template #fallback><p class="mt-1 whitespace-pre-wrap break-words text-sm">{{ byId.get(message.id)!.text }}</p></template></Suspense>
            <p v-else class="mt-1 whitespace-pre-wrap break-words text-sm text-white">{{ byId.get(message.id)!.text }}</p>
            <div v-if="byId.get(message.id)!.proposal" class="mt-3 rounded-lg border border-line p-3" data-proposal>
              <p class="text-xs text-muted">Proposed decision</p>
              <p class="mt-1 whitespace-pre-wrap break-words text-sm text-white/85">{{ byId.get(message.id)!.proposal!.reason }}</p>
              <p v-if="byId.get(message.id)!.receipt" role="status" class="mt-2 text-sm text-mint-200">Decision submitted. The issue status above shows whether Forge accepted it.</p>
              <UButton
                v-else
                class="mt-2"
                size="sm"
                :color="byId.get(message.id)!.proposal!.action === primary ? 'primary' : 'neutral'"
                :variant="byId.get(message.id)!.proposal!.action === primary ? 'solid' : 'outline'"
                :disabled="disabled || busy"
                data-proposal-action
                @click="emit('act', byId.get(message.id)!.proposal!.action, byId.get(message.id)!.proposal!.reason)"
              >{{ ACTION_LABELS[byId.get(message.id)!.proposal!.action] }}</UButton>
            </div>
            <div v-if="message.role === 'assistant' && actions.length" class="mt-2 flex flex-wrap items-center gap-1.5" data-reply-actions>
              <span class="text-xs text-muted">You can:</span>
              <UButton
                v-for="action in actions"
                :key="action"
                size="xs"
                color="neutral"
                variant="outline"
                class="rounded-full"
                :disabled="disabled || busy"
                @click="emit('act', action, '')"
              >{{ ACTION_LABELS[action] }}</UButton>
            </div>
          </template>
        </template>
      </UChatMessages>
      <!-- While sending or while the agent works, the chat's own indicators
           (loading submit, typing dots, streamed text) carry this visually;
           the text stays for assistive technology. -->
      <p role="status" :class="sending || busy ? 'sr-only' : 'text-sm text-muted'">{{ sending ? 'Sending…' : poll.data.value?.reason }}</p>
      <p v-if="error || poll.error.value" role="alert" class="text-sm text-amber-200">{{ error || poll.error.value }}</p>
      <label class="text-sm" :for="`question-${issueKey}`">Ask about this issue</label>
      <!-- Enter sends, Shift+Enter adds a line (UChatPrompt's built-in handling). -->
      <UChatPrompt
        :id="`question-${issueKey}`"
        v-model="question"
        :rows="3"
        :maxrows="12"
        maxlength="4000"
        :autofocus="false"
        :disabled="sending || busy"
        placeholder="Why did the last attempt fail? What would unblock it?"
        class="rounded-lg bg-bg ring ring-line"
        :ui="{ base: 'text-base text-white' }"
        @submit="send"
      >
        <template #footer>
          <span class="text-xs text-muted">Enter to send · Shift+Enter for a new line</span>
          <UChatPromptSubmit
            size="sm"
            :status="promptStatus"
            :loading="sending"
            :disabled="promptStatus === 'ready' ? sending || !question.trim() : sending"
            :aria-label="submitLabel"
            @stop="cancel"
            @reload="send"
          />
        </template>
      </UChatPrompt>
    </div>
  </section>
</template>
