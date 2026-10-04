<script setup lang="ts">
import { requestId } from '~/utils/requestId'
import { ACTION_LABELS, type IssueAction } from '~/utils/issues'
import { SUGGESTED_PROMPTS, nearBottom, replyState, unansweredQuestion, unreadReply } from '~/utils/discussion'
interface Message { id: string; sequence: number; role: 'user' | 'assistant'; text: string; proposal?: { action: IssueAction; reason: string } | null; receipt?: string }
interface Conversation { messages: Message[]; before: number | null; status: string; provider?: string; reason: string | null; partial: string; origin: { role: string; provider: string; link: string } | null }
interface ChatEntry { id: string; role: 'user' | 'assistant'; parts: { type: 'text'; text: string }[] }
// The issue panel's Discussion tab: a full-height chat about this issue. The
// thread scrolls, the prompt stays pinned at the bottom. It stays mounted
// while the operator is on Details, so an unsent draft and the scroll
// position survive a switch, and it polls only while it is shown or while a
// reply is in progress (which the tab's dot reports through `attention`).
// `actions` are the decisions the panel offers right now and `primary` the
// one it leads with: the agent's proposal and the chips under each reply
// hand the choice back to the panel (`act`), which runs the very same
// confirm dialog as its own buttons.
const props = withDefaults(defineProps<{ issueKey: string; active?: boolean; actions?: IssueAction[]; primary?: IssueAction | null; disabled?: boolean }>(), { active: false, actions: () => [], primary: null, disabled: false })
const emit = defineEmits<{ act: [action: IssueAction, reason: string]; attention: [value: { replying: boolean; unread: boolean }] }>()
const api = useForgeApi()
const poll = useResource<Conversation>(() => `/api/issues/discussion?issue=${encodeURIComponent(props.issueKey)}`, path => api.get<Conversation>(path), { interval: 2500 })
const question = ref('')
const error = ref('')
const sendError = ref('')
const sending = ref(false)
const older = ref<Message[]>([])
const olderBefore = ref<number | null | undefined>(undefined)
const before = computed(() => olderBefore.value === undefined ? poll.data.value?.before : olderBefore.value)
const messages = computed(() => [...new Map([...older.value, ...(poll.data.value?.messages ?? [])].map(m => [m.id, m])).values()])
const byId = computed(() => new Map(messages.value.map(m => [m.id, m])))
const state = computed(() => replyState(poll.data.value?.status))
const busy = computed(() => state.value === 'replying' || state.value === 'stopping')
const partial = computed(() => busy.value ? poll.data.value?.partial ?? '' : '')
// The worker's own words for what it is doing ("Preparing a discussion using
// saved issue evidence", "Agent is reading the saved evidence", …).
const statusLine = computed(() => {
  if (sending.value) return 'Sending…'
  return poll.data.value?.reason || (state.value === 'stopping' ? 'Stopping the reply…' : 'Thinking…')
})
// The thread in Nuxt UI's chat message shape. While a reply is on its way, a
// trailing assistant entry carries the streamed text; with no text yet it has
// no parts, and UChatMessages shows its typing indicator in its place.
const PENDING_ID = 'forge-pending-reply'
const pending = computed(() => busy.value || sending.value)
const chatMessages = computed<ChatEntry[]>(() => {
  const thread: ChatEntry[] = messages.value.map(m => ({ id: m.id, role: m.role, parts: [{ type: 'text', text: m.text }] }))
  if (pending.value) thread.push({ id: PENDING_ID, role: 'assistant', parts: partial.value ? [{ type: 'text', text: partial.value }] : [] })
  return thread
})
// What went wrong, shown inline at the end of the thread with a way to try
// again: the send itself, the reply (the worker stopped or ran out of time),
// or reading the conversation at all.
const unanswered = computed(() => unansweredQuestion(messages.value))
const failure = computed<{ text: string; retry: () => void } | null>(() => {
  if (sendError.value) return { text: sendError.value, retry: () => void send() }
  if (state.value === 'failed') return { text: poll.data.value?.reason || 'The reply did not finish.', retry: () => void retryReply() }
  if (poll.error.value) return { text: `Could not load the conversation. ${poll.error.value}`, retry: () => void poll.refresh() }
  return null
})
const empty = computed(() => !messages.value.length && !pending.value && !failure.value)
// Drives UChatPromptSubmit: send while ready, stop (cancel the reply) while
// the agent works, retry with the same message identity after a failed send.
const promptStatus = computed(() => {
  if (busy.value) return partial.value ? 'streaming' : 'submitted'
  if (sendError.value && question.value.trim()) return 'error'
  return 'ready'
})
const submitLabel = computed(() => ({ ready: 'Send', submitted: 'Stop reply', streaming: 'Stop reply', error: 'Retry send' })[promptStatus.value])

// Poll while shown, or while a reply is on its way so the tab can say when it
// lands; nothing polls before the operator first looks.
onMounted(() => watch([() => props.active, busy], ([active, working]) => {
  if (active || working) poll.start(); else poll.stop()
}, { immediate: true }))

// An answer that arrived while the operator was on Details. The first load is
// the baseline, so an old conversation never reads as new.
const latestReply = computed(() => messages.value.findLast(m => m.role === 'assistant')?.id ?? null)
const seen = ref<string | null | undefined>(undefined)
watch([latestReply, () => props.active, () => !!poll.data.value], () => {
  if (poll.data.value && (seen.value === undefined || props.active)) seen.value = latestReply.value
}, { immediate: true })
watch(() => ({ replying: pending.value, unread: unreadReply(latestReply.value, seen.value, props.active) }),
  value => emit('attention', value), { immediate: true, deep: true })

// Scrolling: new content follows the operator down while they read the end of
// the thread; scrolled up, it waits behind a "New messages" button.
const root = ref<HTMLElement | null>(null)
const scrollBox = ref<HTMLElement | null>(null)
const atBottom = ref(true)
const unseenBelow = ref(false)
let savedScroll: number | null = null
const still = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches
function onScroll() {
  const box = scrollBox.value
  if (!box || !props.active) return
  savedScroll = box.scrollTop
  atBottom.value = nearBottom(box)
  if (atBottom.value) unseenBelow.value = false
}
function toBottom(smooth = false) {
  const box = scrollBox.value
  if (!box) return
  box.scrollTo({ top: box.scrollHeight, behavior: smooth && !still() ? 'smooth' : 'auto' })
  savedScroll = box.scrollTop
  atBottom.value = true
  unseenBelow.value = false
}
const signature = computed(() => [messages.value.length, latestReply.value, partial.value.length, pending.value, state.value, !!failure.value].join(':'))
watch(signature, async () => {
  if (!props.active) { if (!atBottom.value) unseenBelow.value = true; return }
  const follow = atBottom.value
  await nextTick()
  if (follow) toBottom()
  else unseenBelow.value = true
})
// Coming back to the tab restores where the operator was reading (the end,
// the first time).
watch(() => props.active, async (active) => {
  if (!active) return
  await nextTick()
  const box = scrollBox.value
  if (!box) return
  if (savedScroll === null || atBottom.value) toBottom()
  else box.scrollTop = savedScroll
})

// The caret waits in the message box whenever the tab opens.
// Returns whether it could: not while the tab is still hidden.
function focus(): boolean {
  const box = root.value?.querySelector<HTMLTextAreaElement>('textarea')
  if (!box || box.disabled || !box.offsetParent) return false
  box.focus({ preventScroll: true })
  const end = box.value.length
  box.setSelectionRange(end, end)
  return document.activeElement === box
}
defineExpose({ focus })

// Retain the identity on transport failure: retry cannot submit the same turn twice.
let draftId: string | null = null
let draftText = ''
async function post(text: string, fromPrompt: boolean) {
  const message = text.trim()
  if (!message || sending.value || busy.value) return
  if (draftText !== message) { draftText = message; draftId = requestId() }
  sending.value = true; error.value = ''; sendError.value = ''
  await nextTick(); toBottom(true)
  try {
    await api.act('/api/issues/discussion', { action: 'send', key: props.issueKey, message: draftText, request_id: draftId })
    if (fromPrompt) question.value = ''
    draftId = null; draftText = ''
    await poll.refresh()
  } catch (e) {
    sendError.value = e instanceof Error ? e.message : 'Could not send. Retry uses the same message identity.'
    if (!fromPrompt) question.value ||= message
  }
  finally { sending.value = false }
}
const send = () => post(question.value, true)
// After a reply that failed or timed out, ask the same question again.
const retryReply = () => unanswered.value ? post(unanswered.value, false) : void focus()
async function cancel() {
  if (poll.data.value?.status === 'cancelling') return
  error.value = ''
  try { await api.act('/api/issues/discussion', { action: 'cancel', key: props.issueKey }); await poll.refresh() }
  catch (e) { error.value = e instanceof Error ? e.message : 'Could not cancel the discussion' }
}
async function loadOlder() {
  if (!before.value) return
  const box = scrollBox.value
  const from = box ? box.scrollHeight - box.scrollTop : 0
  try {
    const page = await api.get<Conversation>(`/api/issues/discussion?issue=${encodeURIComponent(props.issueKey)}&before=${before.value}`)
    older.value = [...page.messages, ...older.value]; olderBefore.value = page.before
    await nextTick()
    if (box) box.scrollTop = box.scrollHeight - from
  } catch (e) { error.value = e instanceof Error ? e.message : 'Could not load earlier messages' }
}
</script>

<template>
  <section id="issue-discussion" ref="root" class="flex h-full min-h-0 min-w-0 flex-col" aria-label="Discussion">
    <!-- Where the answers come from, as the chat's subtitle. -->
    <p id="discussion-context" class="shrink-0 border-b border-line pb-2 text-xs text-muted wrap-anywhere">
      New conversation using saved issue evidence.<span v-if="poll.data.value?.provider"> Replying with {{ poll.data.value.provider }}.</span><template v-if="poll.data.value?.origin"> Latest context: {{ poll.data.value.origin.provider }} · {{ poll.data.value.origin.role }}. <NuxtLink :to="poll.data.value.origin.link" class="underline">View attempt</NuxtLink></template>
    </p>

    <div class="relative min-h-0 flex-1">
      <div
        ref="scrollBox"
        role="log"
        aria-live="polite"
        aria-label="Issue conversation"
        :aria-busy="pending"
        class="h-full overflow-y-auto overscroll-contain px-px py-3"
        @scroll.passive="onScroll"
      >
        <!-- A readable column on a wide page; the whole width in the drawer. -->
        <div class="mx-auto flex min-h-full w-full max-w-3xl flex-col">
          <UButton v-if="before" color="neutral" variant="ghost" size="sm" class="mb-2" icon="i-lucide-history" @click="loadOlder">Earlier messages</UButton>

          <!-- Nothing said yet: what this is for, and a few ways to start. -->
          <div v-if="empty" id="discussion-empty" class="flex flex-1 flex-col items-center justify-center gap-3 px-2 py-6 text-center">
            <UIcon name="i-lucide-messages-square" class="size-7 text-muted" aria-hidden="true" />
            <p class="max-w-sm text-sm text-muted">Discuss why this issue is blocked and what to try next. Applying a proposal submits a decision for Forge to check.</p>
            <div class="flex max-w-full flex-wrap justify-center gap-2" data-suggestions>
              <UButton
                v-for="prompt in SUGGESTED_PROMPTS"
                :key="prompt"
                size="sm"
                color="neutral"
                variant="outline"
                class="max-w-full rounded-full"
                :ui="{ label: 'whitespace-normal text-left' }"
                :disabled="sending"
                @click="post(prompt, false)"
              >{{ prompt }}</UButton>
            </div>
          </div>

          <UChatMessages
            v-else-if="chatMessages.length"
            :messages="chatMessages"
            :status="pending ? 'streaming' : 'ready'"
            :should-scroll-to-bottom="false"
            :should-auto-scroll="false"
            :auto-scroll="false"
            :user="{ side: 'right', variant: 'outline' }"
            :assistant="{ side: 'left', variant: 'naked' }"
            compact
            :ui="{ root: 'min-w-0 gap-3 px-0' }"
          >
            <template #content="{ message }">
              <p class="text-xs font-semibold text-muted">{{ message.role === 'user' ? 'You' : 'Forge agent' }}</p>
              <div v-if="message.id === PENDING_ID" data-streaming class="mt-1">
                <UChatShimmer :text="statusLine" class="text-sm" />
                <p class="mt-1 whitespace-pre-wrap break-words text-sm text-muted">{{ partial }}</p>
              </div>
              <template v-else-if="byId.get(message.id)">
                <Suspense v-if="message.role === 'assistant'"><ConversationMessage :id="message.id" :text="byId.get(message.id)!.text" /><template #fallback><p class="mt-1 whitespace-pre-wrap break-words text-sm">{{ byId.get(message.id)!.text }}</p></template></Suspense>
                <p v-else class="mt-1 whitespace-pre-wrap break-words text-sm text-white">{{ byId.get(message.id)!.text }}</p>
                <div v-if="byId.get(message.id)!.proposal" class="mt-3 rounded-lg border border-line p-3" data-proposal>
                  <p class="text-xs text-muted">Proposed decision</p>
                  <p class="mt-1 whitespace-pre-wrap break-words text-sm text-white/85">{{ byId.get(message.id)!.proposal!.reason }}</p>
                  <p v-if="byId.get(message.id)!.receipt" role="status" class="mt-2 text-sm text-mint-200">Decision submitted. The issue status in Details shows whether Forge accepted it.</p>
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
            <!-- No streamed text yet: the typing dots, with what the worker is doing. -->
            <template #indicator="{ ui }">
              <div data-streaming class="flex flex-col gap-1">
                <div :class="ui.indicator()"><span /><span /><span /></div>
                <UChatShimmer :text="statusLine" class="text-sm" />
              </div>
            </template>
          </UChatMessages>

          <p v-if="state === 'stopped' && !pending" class="mt-2 text-sm text-muted" data-stopped>{{ poll.data.value?.reason || 'Reply stopped' }}</p>
          <div v-if="failure && !pending" role="alert" data-discussion-error class="mt-3 flex flex-wrap items-center gap-2 rounded-lg border border-amber-200/30 bg-amber-200/5 px-3 py-2 text-sm text-amber-200">
            <UIcon name="i-lucide-triangle-alert" class="size-4 shrink-0" aria-hidden="true" />
            <span class="min-w-0 flex-1 wrap-anywhere">{{ failure.text }}</span>
            <UButton size="xs" color="neutral" variant="outline" icon="i-lucide-rotate-ccw" @click="failure.retry">Try again</UButton>
          </div>
          <p v-if="error" role="alert" class="mt-2 text-sm text-amber-200">{{ error }}</p>
        </div>
      </div>

      <UButton
        v-if="unseenBelow && !atBottom"
        size="xs"
        color="neutral"
        variant="solid"
        icon="i-lucide-arrow-down"
        class="absolute bottom-2 left-1/2 -translate-x-1/2 rounded-full shadow-lg"
        data-jump
        @click="toBottom(true)"
      >New messages</UButton>
    </div>

    <!-- The chat's own indicators carry progress visually; this line is for
         assistive technology. -->
    <p role="status" class="sr-only">{{ pending ? statusLine : '' }}</p>
    <label class="sr-only" :for="`question-${issueKey}`">Ask about this issue</label>
    <!-- Enter sends, Shift+Enter adds a line (UChatPrompt's built-in
         handling). The box stays editable while a reply is on its way, so
         the next question can be drafted; only sending waits. -->
    <UChatPrompt
      :id="`question-${issueKey}`"
      v-model="question"
      :rows="2"
      :maxrows="8"
      maxlength="4000"
      :autofocus="false"
      :disabled="sending"
      placeholder="Why did the last attempt fail? What would unblock it?"
      class="mx-auto w-full max-w-3xl shrink-0 rounded-lg bg-bg ring ring-line"
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
  </section>
</template>
