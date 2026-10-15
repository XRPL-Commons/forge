<script setup lang="ts">
import { requestId } from '~/utils/requestId'
import { ACTION_LABELS, type IssueAction } from '~/utils/issues'
import { SUGGESTED_PROMPTS, collapsible, elapsedLabel, slowNote, findingDetail, leftLine, liveLine, nearBottom, presenceMood, proposalState, canUndoProposal, replyState, threadEntries, unansweredQuestion, unreadReply, type ChatRole, type Presence, type ThreadEntry, type ThreadMessage } from '~/utils/discussion'
import { persona } from '~/utils/personas'
interface Message extends ThreadMessage { proposal?: { action: IssueAction; reason: string } | null }
interface Conversation { messages: Message[]; before: number | null; status: string; provider?: string; reason: string | null; partial: string; origin: { role: string; provider: string; link: string } | null; presence?: Presence | null }
interface ChatEntry { id: string; role: ChatRole; parts: { type: 'text'; text: string }[]; ui?: Record<string, string> }
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
const emit = defineEmits<{ act: [action: IssueAction, reason: string, messageId?: string, choiceId?: string]; attention: [value: { replying: boolean; unread: boolean }] }>()
function chooseMessage(messageId: string, selection: import('~/utils/steward').StewardSelection) {
  const message = byId.value.get(messageId)
  const choice = message?.steward_decision?.choices.find(value => value.id === selection.choice_id)
  if (!choice || choice.action === 'answer' || !(choice.action in ACTION_LABELS)) return
  const reason = 'logins' in choice.parameters ? (choice.parameters as { logins: string[] }).logins.join(', ') : choice.reason
  emit('act', choice.action as IssueAction, reason, messageId, choice.id)
}

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
// Who is in the conversation: Forge looking for someone available, an agent
// that joined and what it is doing, or why it left.
const presence = computed<Presence | null>(() => poll.data.value?.presence ?? null)
const now = useClock(1000)
const live = computed(() => busy.value && (presence.value?.phase === 'finding' || presence.value?.phase === 'joined') ? presence.value : null)
const liveAgent = computed(() => live.value?.phase === 'joined' ? persona(live.value.agent?.provider) : null)
const activity = computed(() => live.value?.phase === 'joined' ? live.value.activity ?? [] : [])
const waitedFor = computed(() => {
  const since = live.value?.phase === 'joined' ? live.value.joined_at : live.value?.waiting_since
  return since ? elapsedLabel(now.value - since) : ''
})
// How long the agent has worked on this answer, and a gentle note past ~90 s.
const slow = computed(() => {
  if (live.value?.phase !== 'joined' || !live.value.joined_at) return null
  return slowNote(live.value.mode, now.value - live.value.joined_at)
})
// Long answers start folded; the operator unfolds the ones they want.
const unfolded = ref(new Set<string>())
const folded = (id: string) => !unfolded.value.has(id) && collapsible(byId.value.get(id)?.text ?? '')
function toggle(id: string) {
  const next = new Set(unfolded.value)
  if (next.has(id)) next.delete(id); else next.add(id)
  unfolded.value = next
}
// One line for what is happening now: the shimmer and the screen reader say it.
const statusLine = computed(() => {
  if (sending.value) return 'Sending…'
  if (state.value === 'stopping') return `Stopping ${liveAgent.value?.name ?? 'the reply'}…`
  if (liveAgent.value) return liveLine(live.value?.agent, activity.value, live.value?.instead_of)
  if (live.value?.phase === 'finding') return 'Looking for someone available…'
  return poll.data.value?.reason || 'Thinking…'
})
// The thread in Nuxt UI's chat message shape. Who left without answering is
// a system line, not a bubble; while a reply is on its way, a trailing system
// entry shows the one live status line, which the answer then replaces.
const PENDING_ID = 'forge-pending-reply'
const pending = computed(() => busy.value || sending.value)
const entries = computed(() => threadEntries(messages.value, pending.value))
const entryById = computed(() => new Map<string, ThreadEntry>(entries.value.map(e => [e.id, e])))
const SYSTEM_UI = { leading: 'hidden', container: 'w-full justify-center', body: 'w-full' }
const chatMessages = computed<ChatEntry[]>(() => {
  const thread: ChatEntry[] = entries.value.map((entry) => {
    if (entry.kind === 'user') return { id: entry.id, role: 'user', parts: [{ type: 'text', text: entry.message.text }], ui: { leading: 'hidden' } }
    if (entry.kind === 'answer') return { id: entry.id, role: 'assistant', parts: [{ type: 'text', text: entry.message.text }] }
    return { id: entry.id, role: 'system', parts: [{ type: 'text', text: entry.message.text }], ui: SYSTEM_UI }
  })
  if (pending.value) thread.push({ id: PENDING_ID, role: 'system', parts: [{ type: 'text', text: statusLine.value }], ui: SYSTEM_UI })
  return thread
})
// The conversation's subtitle names who answers here.
const speaker = computed(() => {
  const agent = live.value?.agent ?? messages.value.findLast(m => m.role === 'assistant' && m.agent)?.agent
  const provider = agent?.provider ?? poll.data.value?.provider
  return provider ? persona(provider) : null
})
const lastLeft = computed(() => messages.value.findLast(m => m.role === 'system') ?? null)
const actionable = (id: string) => { const entry = entryById.value.get(id); return entry?.kind === 'left' && entry.actionable }
// What went wrong, shown inline at the end of the thread with a way to try
// again: the send itself, the reply (the worker stopped or ran out of time),
// or reading the conversation at all.
const unanswered = computed(() => unansweredQuestion(messages.value))
const failure = computed<{ text: string; retry: () => void } | null>(() => {
  if (sendError.value) return { text: sendError.value, retry: () => void send() }
  // A conversation from before presence: no left line says why, so say it here.
  if (state.value === 'failed' && !lastLeft.value) return { text: poll.data.value?.reason || 'The reply did not finish.', retry: () => void retryReply() }
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
const signature = computed(() => [messages.value.length, latestReply.value, partial.value.length, pending.value, state.value, !!failure.value, live.value?.phase, activity.value.length].join(':'))
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
// Request a pinned reversal. Only the refreshed canonical receipt can say reopened.
const undoing = ref<string | null>(null)
async function undo(id: string) {
  if (undoing.value || !byId.value.has(id) || !canUndoProposal(byId.value.get(id)!)) return
  undoing.value = id; error.value = ''
  try { await api.act('/api/issues/discussion', { action: 'undo', key: props.issueKey, message_id: id }); await poll.refresh() }
  catch (e) { error.value = e instanceof Error ? e.message : 'Could not reopen the issue' }
  finally { undoing.value = null }
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
      New conversation using saved issue evidence.<span v-if="speaker" data-speaker>{{ ` ${speaker.name} answers here, via ${speaker.providerLabel}.` }}</span><template v-if="poll.data.value?.origin"> Latest context: {{ poll.data.value.origin.provider }} · {{ poll.data.value.origin.role }}. <NuxtLink :to="poll.data.value.origin.link" class="underline">View attempt</NuxtLink></template>
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
            <!-- An answer carries its agent's face. -->
            <template #leading="{ message }">
              <AgentAvatar v-if="entryById.get(message.id)?.kind === 'answer'" :provider="byId.get(message.id)?.agent?.provider ?? null" mood="happy" :size="28" />
            </template>
            <template #content="{ message }">
              <!-- Live: Forge looking for someone available, or who joined and what it is doing. -->
              <div v-if="message.id === PENDING_ID" data-streaming data-presence :data-phase="live?.phase ?? 'sending'" class="mx-auto flex w-full max-w-xl items-start gap-3 rounded-lg border border-line/70 px-3 py-2.5">
                <AgentAvatar :provider="liveAgent?.provider ?? null" :mood="live ? presenceMood(live.phase) : 'looking'" :size="40" />
                <div class="min-w-0 flex-1">
                  <!-- One live line, updated in place: what the agent is doing now. -->
                  <p v-if="liveAgent && live" class="text-sm wrap-anywhere" data-presence-title :data-answer-mode="live.mode ?? 'evidence'">
                    <UChatShimmer v-if="state !== 'stopping'" :text="statusLine" data-current />
                    <span v-else class="text-muted">{{ statusLine }}</span>
                    <span v-if="waitedFor" class="text-muted" data-elapsed> · {{ waitedFor }}<template v-if="slow">, {{ slow }}</template></span>
                  </p>
                  <template v-else>
                    <UChatShimmer :text="live?.phase === 'finding' ? 'Looking for someone available…' : statusLine" class="text-sm" data-presence-title />
                    <p v-if="live?.phase === 'finding'" class="mt-1 text-xs text-muted wrap-anywhere" data-finding-reason>
                      <template v-if="live.instead_of">{{ persona(live.instead_of.provider).name }} left · </template>{{ findingDetail(live) }}<span v-if="waitedFor"> · waiting {{ waitedFor }}</span>
                      <NuxtLink v-if="live.link" :to="live.link.to" class="ml-1 underline">{{ live.link.label }}</NuxtLink>
                    </p>
                  </template>
                  <p v-if="partial" class="mt-1 whitespace-pre-wrap break-words text-sm text-muted">{{ partial }}</p>
                </div>
              </div>
              <!-- Who left without answering, and why. -->
              <div v-else-if="entryById.get(message.id)?.kind === 'left'" data-left :data-reason="byId.get(message.id)?.reason" class="mx-auto flex max-w-xl flex-wrap items-center justify-center gap-2 text-center text-sm text-muted">
                <AgentAvatar v-if="byId.get(message.id)?.agent" :provider="byId.get(message.id)!.agent!.provider" mood="left" :size="28" />
                <span class="min-w-0 wrap-anywhere">{{ leftLine(byId.get(message.id)!) }}<template v-if="byId.get(message.id)!.retrying"> · finding someone else</template></span>
                <NuxtLink v-if="byId.get(message.id)!.link" :to="byId.get(message.id)!.link!.to" class="underline">{{ byId.get(message.id)!.link!.label }}</NuxtLink>
                <UButton v-if="actionable(message.id)" size="xs" color="neutral" variant="outline" icon="i-lucide-rotate-ccw" data-try-again @click="retryReply">Try again</UButton>
              </div>
              <template v-else-if="byId.get(message.id)">
                <p class="text-xs font-semibold text-muted">
                  <template v-if="message.role === 'user'">You</template>
                  <template v-else-if="byId.get(message.id)!.agent"><span class="text-white/85" data-persona-name>{{ persona(byId.get(message.id)!.agent!.provider).name }}</span> <span class="font-normal">via {{ persona(byId.get(message.id)!.agent!.provider).providerLabel }}</span></template>
                  <template v-else>Forge agent</template>
                </p>
                <!-- A chat reply: anything past about six lines folds behind Show more. -->
                <div v-if="message.role === 'assistant'" :id="`answer-${message.id}`" data-answer :data-collapsed="folded(message.id) ? 'true' : 'false'" :class="folded(message.id) ? 'max-h-32 overflow-hidden [mask-image:linear-gradient(to_bottom,#000_60%,transparent)]' : ''">
                  <Suspense><ConversationMessage :id="message.id" :text="byId.get(message.id)!.text" /><template #fallback><p class="mt-1 whitespace-pre-wrap break-words text-sm">{{ byId.get(message.id)!.text }}</p></template></Suspense>
                </div>
                <p v-else class="mt-1 whitespace-pre-wrap break-words text-sm text-white">{{ byId.get(message.id)!.text }}</p>
                <UButton
                  v-if="message.role === 'assistant' && collapsible(byId.get(message.id)!.text)"
                  variant="link"
                  color="neutral"
                  size="xs"
                  class="px-0"
                  :aria-expanded="!folded(message.id)"
                  :aria-controls="`answer-${message.id}`"
                  data-show-more
                  @click="toggle(message.id)"
                >{{ folded(message.id) ? 'Show more' : 'Show less' }}</UButton>
                <!-- Request progress comes from canonical action receipts, not worker prose. -->
                <RankedChoices v-if="byId.get(message.id)!.steward_decision" :decision="byId.get(message.id)!.steward_decision!"
                  :selected-choice="byId.get(message.id)!.selected_choice_id" :disabled="disabled || busy || !!byId.get(message.id)!.selected_choice_id || !!byId.get(message.id)!.requested"
                  @select="chooseMessage(message.id, $event)" />
                <div v-if="byId.get(message.id)!.proposal" class="mt-2 flex w-fit max-w-full flex-wrap items-center gap-x-3 gap-y-1.5 rounded-lg border border-line px-3 py-2" data-proposal :data-state="proposalState(byId.get(message.id)!)" aria-label="Suggested decision" role="group">
                  <p v-if="proposalState(byId.get(message.id)!) === 'closed'" role="status" class="text-sm text-mint-200">Closed as no longer needed.</p>
                  <p v-else-if="proposalState(byId.get(message.id)!) === 'reopened'" role="status" class="text-sm text-muted">Reopened. Forge checks the issue on GitHub again.</p>
                  <p v-else-if="proposalState(byId.get(message.id)!) === 'undo_pending'" role="status" class="text-sm text-muted">Reopening requested. Waiting for GitHub confirmation.</p>
                  <p v-else-if="proposalState(byId.get(message.id)!) === 'undo_failed'" role="status" class="text-sm text-amber-200">Could not reopen: {{ byId.get(message.id)!.action_state!.undo!.message || 'The request was rejected.' }}</p>
                  <p v-else-if="proposalState(byId.get(message.id)!) === 'failed'" role="status" class="text-sm text-amber-200">Decision not applied: {{ byId.get(message.id)!.action_state!.message || 'The request was rejected.' }}</p>
                  <p v-else-if="proposalState(byId.get(message.id)!) === 'pending'" role="status" class="text-sm text-muted">Decision requested. {{ byId.get(message.id)!.action_state!.message || 'Waiting for GitHub confirmation.' }}</p>
                  <p v-else-if="proposalState(byId.get(message.id)!) === 'applied'" role="status" class="text-sm text-mint-200">Decision applied.</p>
                  <p v-else-if="proposalState(byId.get(message.id)!) === 'submitted'" role="status" class="text-sm text-muted">Decision submitted. Its outcome has not been confirmed here.</p>
                  <UButton
                    v-else-if="!byId.get(message.id)!.steward_decision"
                    class="shrink-0"
                    size="sm"
                    :color="byId.get(message.id)!.proposal!.action === 'close' ? 'error' : byId.get(message.id)!.proposal!.action === primary ? 'primary' : 'neutral'"
                    :variant="byId.get(message.id)!.proposal!.action === primary ? 'solid' : 'outline'"
                    :disabled="disabled || busy"
                    data-proposal-action
                    @click="emit('act', byId.get(message.id)!.proposal!.action, byId.get(message.id)!.proposal!.reason, message.id)"
                  >{{ byId.get(message.id)!.proposal!.action === 'close' ? `${ACTION_LABELS.close}…` : ACTION_LABELS[byId.get(message.id)!.proposal!.action] }}</UButton>
                  <UButton v-if="canUndoProposal(byId.get(message.id)!)" class="shrink-0" size="xs" color="neutral" variant="outline" icon="i-lucide-undo-2" :loading="undoing === message.id" :disabled="!!undoing || disabled" data-undo @click="undo(message.id)">Undo</UButton>
                  <p class="line-clamp-2 min-w-0 flex-1 basis-48 text-xs text-muted wrap-anywhere" data-proposal-reason :title="byId.get(message.id)!.proposal!.reason">{{ byId.get(message.id)!.proposal!.reason }}</p>
                  <p v-if="byId.get(message.id)!.close_error && proposalState(byId.get(message.id)!) === 'offer'" class="basis-full text-xs text-amber-200 wrap-anywhere" data-close-error>Forge could not close it for you: {{ byId.get(message.id)!.close_error }}</p>
                </div>
                <p v-else-if="byId.get(message.id)!.unavailable" class="mt-1 text-xs text-muted" data-unavailable>That action isn't available here.</p>
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

          <p v-if="state === 'stopped' && !pending && !lastLeft" class="mt-2 text-sm text-muted" data-stopped>{{ poll.data.value?.reason || 'Reply stopped' }}</p>
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
