// The issue panel's two tabs and the discussion chat's small decisions. All
// pure: the components turn these into router.replace() calls and classes.
import type { PanelQuery, QueryValue } from './panel'

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
// that never got an answer, i.e. the last message when it is theirs.
export function unansweredQuestion(messages: readonly { role: 'user' | 'assistant'; text: string }[]): string | null {
  const last = messages[messages.length - 1]
  return last?.role === 'user' ? last.text : null
}

export const SUGGESTED_PROMPTS = [
  'Why did the last attempt fail?',
  'What would unblock this?',
  'What can I do from here?',
] as const
