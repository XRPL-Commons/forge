// What the factory line's hover card says about one cube or one crate bay,
// and where the card goes on screen. Pure, so the strip only renders it.
import { elapsed, type LedgerIssueView } from './factory'
import {
  LINE_REASON_SHORT, LINE_REASON_TEXT, LINE_STAGE_LABELS, lineFill,
  type AttentionReason, type LineItem,
} from './factoryLine'

interface LineIssueCard {
  kind: 'issue'
  // The crate bay it sits in, for the bay's character.
  reason: AttentionReason | null
  ref: string
  title: string
  // The status and reason in plain words: "Waiting on #90, #101".
  status: string
  // The longer reason behind it, when Forge recorded one.
  detail: string
  stage: string
  swatch: string
  unblocks: number
  hint: string
}
interface LineListCard {
  kind: 'list'
  reason: AttentionReason | null
  heading: string
  sub: string
  rows: { id: string; ref: string; title: string }[]
  more: number
  hint: string
}
export type LineCard = LineIssueCard | LineListCard

const ATTEMPT_LIMIT = 3
const LIST_LIMIT = 6
const VERBS: Record<string, string> = { build: 'Building', review: 'Reviewing', audit: 'Auditing' }

function lineRef(item: Pick<LineItem, 'number' | 'id'>): string {
  return item.number != null ? `#${item.number}` : item.id
}

export function lineStatus(item: LineItem, issue?: LedgerIssueView): string {
  if (item.stage === 'attention' && item.reason) return LINE_REASON_TEXT[item.reason]
  if (item.stage === 'waiting') return item.on?.length ? `Waiting on ${item.on.map(n => `#${n}`).join(', ')}` : 'Waiting on other issues'
  if (item.stage === 'queued') return `Queued for ${(item.next ?? 'build') === 'audit' ? 'security audit' : item.next ?? 'build'}`
  const verb = VERBS[item.stage]
  if (verb) {
    const card = item.card
    const time = item.running && card.timerStart !== null && card.timerEnd !== null ? elapsed(card.timerEnd - card.timerStart) : ''
    return item.running ? [verb, time].filter(Boolean).join(' · ') : item.status || verb
  }
  return item.status || LINE_STAGE_LABELS[item.stage]
}

// What lies behind an issue that needs the operator: how many attempts
// failed, then Forge's own summary of it.
function plainDetail(item: LineItem, issue: LedgerIssueView | undefined, status: string): string {
  if (item.stage !== 'attention' && item.stage !== 'hold') return ''
  const failures = issue?.failure_count ?? 0
  const tried = failures && (item.reason === 'failed' || item.reason === 'approach') ? `Failed ${failures} of ${ATTEMPT_LIMIT}` : ''
  const text = (issue?.last_summary?.headline || issue?.reason || '').trim()
  return [tried, text && text !== status ? text : ''].filter(Boolean).join(' · ')
}

export function lineIssueCard(item: LineItem, issue: LedgerIssueView | undefined, unblocks: number, showRepo = true): LineIssueCard {
  const status = lineStatus(item, issue)
  return {
    kind: 'issue',
    reason: item.stage === 'attention' ? item.reason ?? null : null,
    ref: showRepo ? `${item.repo} · ${lineRef(item)}` : lineRef(item),
    title: item.card.title || issue?.title || '',
    status,
    detail: plainDetail(item, issue, status),
    stage: LINE_STAGE_LABELS[item.stage],
    swatch: lineFill(item),
    unblocks: Math.max(0, unblocks),
    hint: 'Click to open',
  }
}

export function lineListCard(heading: string, items: LineItem[], hint = 'Click to see them all', sub = '', reason: AttentionReason | null = null): LineListCard {
  return {
    kind: 'list',
    reason,
    heading,
    sub,
    rows: items.slice(0, LIST_LIMIT).map(item => ({ id: item.id, ref: lineRef(item), title: item.card.title })),
    more: Math.max(0, items.length - LIST_LIMIT),
    hint,
  }
}

export function lineBayCard(reason: AttentionReason, items: LineItem[]): LineListCard {
  const list = items.filter(item => item.reason === reason)
  return lineListCard(`${list.length} · ${LINE_REASON_SHORT[reason]}`, list, 'Click to see them', LINE_REASON_TEXT[reason], reason)
}

// Where a card of `size` goes next to `anchor` (both in viewport pixels):
// above it when it fits, else below, else whichever side has more room;
// always inside the viewport with `margin` to spare.
export interface Rect { x: number; y: number; w: number; h: number }
interface Placement { left: number; top: number; side: 'above' | 'below' }
export function placeCard(anchor: Rect, size: { w: number; h: number }, viewport: { w: number; h: number }, margin = 8, gap = 8): Placement {
  const room = { above: anchor.y - gap - margin, below: viewport.h - (anchor.y + anchor.h) - gap - margin }
  const side: Placement['side'] = size.h <= room.above ? 'above' : size.h <= room.below ? 'below' : room.above >= room.below ? 'above' : 'below'
  const rawTop = side === 'above' ? anchor.y - gap - size.h : anchor.y + anchor.h + gap
  const top = Math.max(margin, Math.min(rawTop, viewport.h - margin - size.h))
  const rawLeft = anchor.x + anchor.w / 2 - size.w / 2
  const left = Math.max(margin, Math.min(rawLeft, viewport.w - margin - size.w))
  return { left: Math.round(left), top: Math.round(top), side }
}
