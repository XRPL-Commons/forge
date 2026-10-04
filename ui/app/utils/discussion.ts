// The issue panel's two tabs and the discussion chat's small decisions. All
// pure: the components turn these into router.replace() calls and classes.
import type { PanelQuery, QueryValue } from './panel'
import type { AgentMood } from './cube'
import { modelLabel, persona } from './personas'

export type IssueTab = 'details' | 'discussion'

function first(value: QueryValue): string | null {
  const found = Array.isArray(value) ? value[0] : value
  return typeof found === 'string' && found ? found : null
}

// `?tab=discussion` opens the chat; anything else (or nothing) is Details.
export function issueTab(query: PanelQuery | null | undefined): IssueTab {
  return first(query?.tab) === 'discussion' ? 'discussion' : 'details'
}

// The query for `tab`, keeping every other parameter. Details is the
// default, so it leaves the parameter out and the plain issue URL stays clean.
export function tabQuery(query: PanelQuery | null | undefined, tab: IssueTab): PanelQuery {
  const next: PanelQuery = { ...(query ?? {}) }
  delete next.tab
  if (tab === 'discussion') next.tab = 'discussion'
  return next
}

// Whether the operator is reading the end of the thread: new content then
// follows them down; otherwise it waits behind a "New messages" button.
export function nearBottom(box: { scrollTop: number; clientHeight: number; scrollHeight: number }, threshold = 48): boolean {
  return box.scrollHeight - (box.scrollTop + box.clientHeight) <= threshold
}

// The server's discussion status, as the chat shows it.
export type ReplyState = 'idle' | 'replying' | 'stopping' | 'stopped' | 'failed'
export function replyState(status: string | null | undefined): ReplyState {
  switch (status) {
    case 'queued':
    case 'running': return 'replying'
    case 'cancelling': return 'stopping'
    case 'cancelled': return 'stopped'
    case 'unavailable': return 'failed'
    default: return 'idle'
  }
}

// A reply the operator has not seen: the latest answer arrived while they were
// on another tab. `seen` is undefined until the thread first loads, and that
// first load is the baseline, so an old conversation never reads as new.
export function unreadReply(latest: string | null, seen: string | null | undefined, active: boolean): boolean {
  return !active && seen !== undefined && latest !== null && latest !== seen
}

// What "Try again" re-sends after a failed reply: the operator's question
// that never got an answer, i.e. their latest message when nothing but the
// system's own lines ("Ember left: …") came after it. Forge treats the same
// words again as Try again for that question, never as a second copy.
export function unansweredQuestion(messages: readonly { role: ChatRole; text: string }[]): string | null {
  for (let i = messages.length - 1; i >= 0; i--) {
    const message = messages[i]!
    if (message.role === 'assistant') return null
    if (message.role === 'user') return message.text
  }
  return null
}

// --- Presence: someone available joins, works, answers or leaves -----------

export type ChatRole = 'user' | 'assistant' | 'system'
export interface AgentInfo { provider: string | null; model?: string | null; name?: string; label?: string }
export interface ActivityLine { at: number; text: string }
export interface Link { to: string; label: string }
export type PresencePhase = 'finding' | 'joined' | 'answered' | 'left' | 'idle'
export type LeftReason = 'timeout' | 'restart' | 'cancelled' | 'error' | 'auth' | 'quota'
export interface Presence {
  phase: PresencePhase
  // finding
  waiting_since?: number | null
  code?: string
  reason?: string
  until?: number | null
  link?: Link | null
  // joined (instead_of: who left before, when Forge switched providers)
  agent?: AgentInfo | null
  joined_at?: number | null
  activity?: ActivityLine[]
  instead_of?: AgentInfo | null
  // left
  text?: string
  at?: number
}
export interface ThreadMessage {
  id: string
  sequence: number
  role: ChatRole
  text: string
  at?: number
  // assistant
  agent?: AgentInfo | null
  activity?: ActivityLine[]
  instead_of?: AgentInfo | null
  joined_at?: number | null
  // system: the agent left without answering
  event?: 'left'
  reason?: LeftReason
  until?: number | null
  link?: Link | null
  retrying?: boolean
}

// The avatar's face for each phase.
export function presenceMood(phase: PresencePhase | undefined): AgentMood {
  if (phase === 'finding') return 'looking'
  if (phase === 'joined') return 'busy'
  if (phase === 'left') return 'left'
  return 'happy'
}

// "12 s", "2 min", "1 h 5 min": how long the operator has been waiting.
export function elapsedLabel(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds))
  if (s < 60) return `${s} s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m} min`
  return `${Math.floor(m / 60)} h${m % 60 ? ` ${m % 60} min` : ''}`
}

// 23:15 in the operator's own time.
export function clockTime(at: number, locale?: string): string {
  return new Date(at * 1000).toLocaleTimeString(locale ?? [], { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' })
}

// "Ember joined · Claude Sonnet 4.6", or "Ember joined instead" after a switch.
export function joinedLine(agent: AgentInfo | null | undefined, instead?: AgentInfo | null): string {
  const who = persona(agent?.provider)
  const switched = instead && instead.provider !== agent?.provider
  return `${who.name} joined${switched ? ' instead' : ''} · ${modelLabel(agent?.model, agent?.provider)}`
}

// Why Forge is still looking. A time is shown only when it says when someone
// frees up (a quota), never for a login that needs the operator.
export function findingDetail(presence: Presence, locale?: string): string {
  const reason = presence.reason || 'Looking for an available agent'
  const timed = presence.until && presence.code !== 'blocked_auth' && presence.code !== 'not_ready'
  return timed ? `${reason} until ${clockTime(presence.until!, locale)}` : reason
}

// A left line, with when the provider frees up for a quota.
export function leftLine(message: Pick<ThreadMessage, 'text' | 'reason' | 'until'>, locale?: string): string {
  return message.reason === 'quota' && message.until ? `${message.text} until ${clockTime(message.until, locale)}` : message.text
}

// The thread as the chat shows it: before each answer a system line says who
// joined; a left line with nothing after it offers Try again (unless Forge is
// already finding someone else).
export type ThreadEntry =
  | { kind: 'user'; id: string; message: ThreadMessage }
  | { kind: 'joined'; id: string; agent: AgentInfo | null; instead: AgentInfo | null }
  | { kind: 'answer'; id: string; message: ThreadMessage }
  | { kind: 'left'; id: string; message: ThreadMessage; actionable: boolean }
export function threadEntries(messages: readonly ThreadMessage[], pending: boolean): ThreadEntry[] {
  const entries: ThreadEntry[] = []
  messages.forEach((message, index) => {
    if (message.role === 'user') entries.push({ kind: 'user', id: message.id, message })
    else if (message.role === 'system') {
      const last = index === messages.length - 1
      entries.push({ kind: 'left', id: message.id, message, actionable: last && !pending && !message.retrying })
    } else {
      if (message.agent) entries.push({ kind: 'joined', id: `joined-${message.id}`, agent: message.agent, instead: message.instead_of ?? null })
      entries.push({ kind: 'answer', id: message.id, message })
    }
  })
  return entries
}

export const SUGGESTED_PROMPTS = [
  'Why did the last attempt fail?',
  'What would unblock this?',
  'What can I do from here?',
] as const
