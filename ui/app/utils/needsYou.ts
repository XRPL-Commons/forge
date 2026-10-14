// The "need you" list behind the factory line's crate: the issues in the
// crate, most unblocking first, filtered by the bay they sit in.
import type { FactoryView } from './factory'
import { LINE_REASONS, LINE_REASON_SHORT, lineGroups, type AttentionReason, type LineItem, type LineState } from './factoryLine'
import { rankByUnblocks, unblockCounts } from './unblocks'

export type NeedsFilter = 'all' | AttentionReason
export const NEEDS_FILTERS: NeedsFilter[] = ['all', ...LINE_REASONS]
export const NEEDS_FILTER_LABELS: Record<NeedsFilter, string> = { all: 'All', ...LINE_REASON_SHORT }

interface NeedsRow { item: LineItem; reason: AttentionReason; unblocks: number }

export function needsYouRows(state: LineState | null, view: Pick<FactoryView, 'issues'> | null | undefined): NeedsRow[] {
  if (!state) return []
  const counts = unblockCounts(view?.issues ?? [])
  // lineGroups orders the crate by bay; the ranking keeps that for ties.
  const crate = lineGroups(state.items).attention
  return rankByUnblocks(crate, item => item.id, counts).map(item => ({ item, reason: item.reason!, unblocks: counts.get(item.id) ?? 0 }))
}

export function needsCounts(rows: NeedsRow[]): Record<NeedsFilter, number> {
  const counts = Object.fromEntries(NEEDS_FILTERS.map(filter => [filter, 0])) as Record<NeedsFilter, number>
  counts.all = rows.length
  for (const row of rows) counts[row.reason] += 1
  return counts
}

export function needsFiltered(rows: NeedsRow[], filter: NeedsFilter): NeedsRow[] {
  return filter === 'all' ? rows : rows.filter(row => row.reason === filter)
}

// The one thing to do about an issue in the crate; every action is taken in
// the issue's panel.
export function needsAction(reason: AttentionReason): { label: string; decision: boolean } {
  return NEEDS_ACTIONS[reason]
}
const NEEDS_ACTIONS: Record<AttentionReason, { label: string; decision: boolean }> = {
  approach: { label: 'Describe approach…', decision: true },
  question: { label: 'Answer…', decision: true },
  approve: { label: 'Approve…', decision: true },
  failed: { label: 'Retry…', decision: false },
  env: { label: 'Fix setup', decision: false },
  review: { label: 'Review', decision: false },
  verify: { label: 'Verify…', decision: true },
}

export function unblocksLabel(n: number): string {
  return n > 0 ? `Unblocks ${n}` : ''
}
