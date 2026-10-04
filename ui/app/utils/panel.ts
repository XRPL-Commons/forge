// The Activity board's detail panel lives in the URL, so a reload, a shared
// link and the back button all land on the same open panel. `?issue=<key>`
// names the issue; `?job=<id>` names one run of it, the same parameter the
// standalone /issues/<key> route already reads. Everything here is pure: the
// page turns these query objects into router.replace() calls.
import type { CardModel } from './factory'

export type QueryValue = string | null | undefined | (string | null)[]
export type PanelQuery = Record<string, QueryValue>

export interface PanelState { key: string | null; job: string | null }

// Vue Router hands a repeated parameter over as an array; the first value wins.
function first(value: QueryValue): string | null {
  const found = Array.isArray(value) ? value[0] : value
  return typeof found === 'string' && found ? found : null
}

// What the URL says is open. Without an issue there is no panel, and a
// stray `?job=` on its own belongs to nothing.
export function panelState(query: PanelQuery | null | undefined): PanelState {
  const key = first(query?.issue)
  if (!key) return { key: null, job: null }
  return { key, job: first(query?.job) }
}

// The query that opens `panel` (or closes the panel, for null), keeping every
// other parameter the page carries. Both keys are always rewritten, so
// switching issues never leaves the previous issue's run behind; the tab
// (`?tab=discussion`) belongs to the issue it was opened on, so it goes too.
export function panelQuery(query: PanelQuery | null | undefined, panel: { key: string; job?: string | null } | null): PanelQuery {
  const next: PanelQuery = { ...(query ?? {}) }
  delete next.issue
  delete next.job
  delete next.tab
  if (!panel?.key) return next
  next.issue = panel.key
  if (panel.job) next.job = panel.job
  return next
}

// What a card opens: its issue, plus the run itself for an internal job row
// (`id` is "job-<id>"), whose issue key may carry no ledger issue at all.
export function panelTarget(card: Pick<CardModel, 'key' | 'id' | 'jobId'>): { key: string; job: string | null } {
  return { key: card.key, job: card.id.startsWith('job-') ? card.jobId : null }
}

// The API names an issue as `/issues?issue=<key>` in `operator.action_url`.
// On the board that link opens the same panel a card does, and stays on `/`.
export function boardLink(url: string): string {
  const match = /^\/issues\?issue=([^&#]+)$/.exec(url)
  return match ? `/?issue=${match[1]}` : url
}
