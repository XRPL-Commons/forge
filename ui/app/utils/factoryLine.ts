// Pure model behind the factory line strip at the top of the Activity page
// (components/FactoryLine.vue). The strip draws the same issues the board
// groups, one cube per issue, so everything here is derived from
// groupIssues() and the raw ledger fields it does not carry. Motion is never
// simulated: a cube moves only when a poll puts its issue somewhere else.
import {
  COLUMN_ROLES, NEXT_ROLE, groupIssues, mergedToday,
  type CardModel, type ColumnRole, type FactoryView, type IssueGroups, type LedgerIssueView,
} from './factory'

export type LineStage = 'queued' | ColumnRole | 'approval' | 'merged' | 'attention' | 'waiting' | 'hold'
export type AttentionReason = 'failed' | 'decision' | 'approval' | 'env'

export interface LineItem {
  // The issue key ("1:601"), so a cube keeps its identity from poll to poll.
  id: string
  number: number | null
  repo: string
  repoId: number | null
  stage: LineStage
  // A queued issue waits in front of the machine that takes it next; a
  // waiting one resumes there once its dependencies close.
  next?: ColumnRole
  reason?: AttentionReason
  // The open issues a waiting issue depends on, by number.
  on?: number[]
  // A worker for this issue is running right now (not starting, not stale).
  running: boolean
  // The plain-language second line of the tooltip.
  status: string
  card: CardModel
}

export interface LineState {
  items: LineItem[]
  slots: Record<ColumnRole, number>
  merged: number
  repos: number
}

export interface Bottleneck { stage: ColumnRole; waiting: number }
export interface LineInfo {
  necks: Bottleneck[]
  neck: Bottleneck | null
  reasons: Record<AttentionReason, number>
  // Machine slots holding an issue, and how many of them are running.
  occupied: number
  running: number
  slots: number
  attention: number
}
export interface HeadlinePart { t: string; c: 'amber' | 'coral' | 'text' | 'muted' }

export const LINE_STAGES: LineStage[] = ['queued', 'build', 'review', 'audit', 'approval', 'merged', 'attention', 'waiting', 'hold']
export const LINE_REASONS: AttentionReason[] = ['failed', 'decision', 'approval', 'env']
export const LINE_STAGE_LABELS: Record<LineStage, string> = {
  queued: 'Queued', build: 'Build', review: 'Review', audit: 'Security audit', approval: 'Waiting to merge',
  merged: 'Merged', attention: 'Needs attention', waiting: 'Waiting on other issues', hold: 'On hold',
}
export const LINE_SHORT_LABELS: Record<ColumnRole, string> = { build: 'Build', review: 'Review', audit: 'Audit' }
export const LINE_REASON_SHORT: Record<AttentionReason, string> = { failed: 'failed', decision: 'decide', approval: 'approve', env: 'env' }
export const LINE_REASON_GROUP: Record<AttentionReason, string> = {
  failed: 'attempts failed', decision: 'decisions needed', approval: 'approvals needed from you', env: 'environment problems',
}

// How a cube looks at each station. A cube matures as it advances: raw grey
// in the queue, richer hues through build, review and audit, a pale
// near-finished tint at approval, and mint once merged. Colour is the first
// step of that evolution; later ones (accessories, size, expression) belong
// in the same per-stage look. Amber is kept for attention and coral for the
// bottleneck flag, so the progression avoids reds and oranges.
export interface StageLook {
  fill: string
  // A cube at a machine that is not running right now: done, or starting.
  ready: string
}
export const LINE_RESERVED = { amber: '#f0b44c', coral: '#ef7d64' } as const
export const LINE_LOOKS: Record<LineStage, StageLook> = {
  queued: { fill: '#a3aab4', ready: '#b9c0ca' },
  build: { fill: '#4fc3d6', ready: '#8ad8e5' },
  review: { fill: '#6a97ee', ready: '#9cbaf4' },
  audit: { fill: '#a58bef', ready: '#c3b1f5' },
  approval: { fill: '#d3e4ef', ready: '#e4eef5' },
  merged: { fill: '#7fd6a0', ready: '#a8ebc3' },
  attention: { fill: LINE_RESERVED.amber, ready: LINE_RESERVED.amber },
  waiting: { fill: '#59606b', ready: '#59606b' },
  hold: { fill: '#3a3f47', ready: '#3a3f47' },
}

function channels(hex: string): [number, number, number] {
  const n = Number.parseInt(hex.slice(1), 16)
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255]
}

// Linear blend of two #rrggbb colours, t from 0 (a) to 1 (b).
export function lineMix(a: string, b: string, t: number): string {
  const x = channels(a), y = channels(b), k = Math.max(0, Math.min(1, t))
  return '#' + x.map((v, i) => Math.round(v + (y[i]! - v) * k).toString(16).padStart(2, '0')).join('')
}

// The colour a cube is drawn in. A waiting cube keeps a faded tint of the
// stage it will resume at.
export function lineFill(item: Pick<LineItem, 'stage' | 'next' | 'running'>): string {
  const look = LINE_LOOKS[item.stage]
  if (item.stage === 'waiting' && item.next) return lineMix(look.fill, LINE_LOOKS[item.next].fill, 0.28)
  if ((item.stage === 'build' || item.stage === 'review' || item.stage === 'audit') && !item.running) return look.ready
  return look.fill
}

// A deliberate pause sleeps on the shelf; every other hold is a question
// for the operator, sorted into the crate by what it asks.
const PAUSED = /Paused by an operator|Not queued|hold or dependency label/
const ENVIRONMENT = /environment needs attention/

export function lineReason(issue: LedgerIssueView): AttentionReason | null {
  if (issue.state === 'approval_needed') return 'approval'
  if (issue.state === 'failed') return 'failed'
  if (issue.state !== 'hold') return null
  const reason = issue.reason ?? ''
  if (PAUSED.test(reason)) return null
  if (issue.prerequisite_hold || ENVIRONMENT.test(reason)) return 'env'
  return 'decision'
}

function attentionStatus(issue: LedgerIssueView, reason: AttentionReason, card: CardModel): string {
  if (reason === 'failed') return issue.failure_count > 0 ? `${issue.failure_count} of 3 attempts failed` : (issue.reason || 'Attempts failed')
  if (reason === 'approval') return issue.reviewers_requested?.logins?.length ? card.reason : 'Approval needed from you'
  if (reason === 'env') return 'Environment problem'
  return 'Decision needed'
}

function columnStatus(card: CardModel, stage: LineStage): string {
  if (stage === 'queued') return 'Waiting for a worker'
  return card.phaseLabel
}

export function lineState(view: FactoryView | null | undefined, now: number, grouped?: IssueGroups): LineState | null {
  if (!view) return null
  const groups = grouped ?? groupIssues(view, now)
  const byKey = new Map((view.issues ?? []).map(issue => [issue.key, issue]))
  const items: LineItem[] = []
  const add = (card: CardModel, stage: LineStage, extra: Partial<LineItem> = {}) => {
    const issue = byKey.get(card.key)
    items.push({
      id: card.key, number: card.number, repo: card.repo, repoId: issue?.repo_id ?? null,
      stage, running: card.running, status: card.phaseLabel, card, ...extra,
    })
  }
  for (const role of COLUMN_ROLES) {
    for (const card of groups.columns[role]) {
      const stage: LineStage = card.jobId ? role : 'queued'
      add(card, stage, { next: stage === 'queued' ? role : undefined, status: columnStatus(card, stage) })
    }
  }
  for (const card of groups.finishing) add(card, 'approval', { status: card.phaseLabel === 'Merging' ? 'Merging' : 'Approved, waiting to merge' })
  for (const card of [...groups.attention, ...groups.held]) {
    const issue = byKey.get(card.key)
    const reason = issue ? lineReason(issue) : null
    if (reason && issue) add(card, 'attention', { reason, status: attentionStatus(issue, reason, card) })
    else add(card, 'hold', { status: 'On hold' })
  }
  for (const card of groups.owned) add(card, 'hold', { status: 'Owned by someone else' })
  for (const card of groups.waiting) {
    const on = byKey.get(card.key)?.blocked_dependencies ?? []
    const state = byKey.get(card.key)?.state ?? ''
    add(card, 'waiting', { on: [...on], next: NEXT_ROLE[state], status: card.phaseLabel })
  }
  const slots = view.runtime?.slots ?? {}
  return {
    items,
    slots: { build: Number(slots.build) || 0, review: Number(slots.review) || 0, audit: Number(slots.audit) || 0 },
    merged: mergedToday(view, now),
    repos: new Set(items.map(item => item.repo)).size,
  }
}

export function lineGroups(items: LineItem[]): Record<LineStage, LineItem[]> {
  const groups = Object.fromEntries(LINE_STAGES.map(stage => [stage, [] as LineItem[]])) as Record<LineStage, LineItem[]>
  for (const item of items) groups[item.stage].push(item)
  groups.attention.sort((a, b) => LINE_REASONS.indexOf(a.reason!) - LINE_REASONS.indexOf(b.reason!))
  return groups
}

// A bottleneck is a stage whose slots are all taken while issues wait in
// front of it. The worst one is named in the headline.
export function lineAnalyse(state: LineState): LineInfo {
  const groups = lineGroups(state.items)
  const necks: Bottleneck[] = []
  for (const stage of COLUMN_ROLES) {
    const waiting = groups.queued.filter(item => item.next === stage).length
    if (waiting > 0 && groups[stage].length >= state.slots[stage]) necks.push({ stage, waiting })
  }
  necks.sort((a, b) => b.waiting - a.waiting)
  const reasons = Object.fromEntries(LINE_REASONS.map(reason => [reason, 0])) as Record<AttentionReason, number>
  for (const item of groups.attention) reasons[item.reason!] += 1
  const working = [...groups.build, ...groups.review, ...groups.audit]
  return {
    necks, neck: necks[0] ?? null, reasons,
    occupied: working.length,
    running: working.filter(item => item.running).length,
    slots: COLUMN_ROLES.reduce((total, stage) => total + state.slots[stage], 0),
    attention: groups.attention.length,
  }
}

export function lineHeadline(info: LineInfo): HeadlinePart[] {
  const parts: HeadlinePart[] = []
  if (info.attention) parts.push({ t: `${info.attention} need${info.attention === 1 ? 's' : ''} you`, c: 'amber' })
  if (info.neck) parts.push({ t: `${LINE_SHORT_LABELS[info.neck.stage]} is the bottleneck, ${info.neck.waiting} waiting`, c: 'coral' })
  else if (info.occupied) parts.push({ t: `${info.occupied} of ${info.slots} slots working`, c: 'text' })
  else if (!info.attention) parts.push({ t: 'All quiet', c: 'muted' })
  return parts
}

export function lineHeadlineText(parts: HeadlinePart[]): string {
  return parts.map(part => part.t).join(' · ')
}

// One sentence for screen readers: the counts the strip draws.
export function lineSummary(state: LineState, info: LineInfo): string {
  const groups = lineGroups(state.items)
  const stage = (role: ColumnRole) => `${LINE_SHORT_LABELS[role]} ${groups[role].length} of ${state.slots[role]}`
  return [
    `Queued ${groups.queued.length}`, ...COLUMN_ROLES.map(stage),
    `waiting to merge ${groups.approval.length}`, `merged today ${state.merged}`,
    `need you ${info.attention}`, `waiting on other issues ${groups.waiting.length}`, `on hold ${groups.hold.length}`,
  ].join(', ') + '.'
}

export function lineTooltip(item: LineItem, showRepo: boolean): string {
  const name = item.number != null ? `#${item.number}` : item.id
  const parts = [name, LINE_STAGE_LABELS[item.stage]]
  if (item.status && item.status !== LINE_STAGE_LABELS[item.stage]) parts.push(item.status)
  if (showRepo) parts.push(item.repo)
  return parts.join(' · ')
}

// Where a cube stands: a queued issue in front of review is not at the same
// station as one in front of build.
export function lineStation(item: Pick<LineItem, 'stage' | 'next'>): string {
  return item.stage === 'queued' ? `queued:${item.next ?? 'build'}` : item.stage
}

export interface LineMove { id: string; from: string; to: string }
export interface LineDiff {
  entered: string[]
  moved: LineMove[]
  left: string[]
  // Issues that left from the approval gate while the merge count went up:
  // they ride the truck instead of fading out.
  merged: string[]
  plus: number
}

export function lineDiff(prev: LineState | null, next: LineState): LineDiff {
  const diff: LineDiff = { entered: [], moved: [], left: [], merged: [], plus: 0 }
  if (!prev) return diff
  const before = new Map(prev.items.map(item => [item.id, item]))
  const after = new Set<string>()
  for (const item of next.items) {
    after.add(item.id)
    const was = before.get(item.id)
    if (!was) diff.entered.push(item.id)
    else if (lineStation(was) !== lineStation(item)) diff.moved.push({ id: item.id, from: lineStation(was), to: lineStation(item) })
  }
  diff.plus = Math.max(0, next.merged - prev.merged)
  let rides = diff.plus
  for (const item of prev.items) {
    if (after.has(item.id)) continue
    if (rides > 0 && item.stage === 'approval') { diff.merged.push(item.id); rides -= 1 }
    else diff.left.push(item.id)
  }
  return diff
}

// The blocker a waiting cube is tied to: the first dependency still on the line.
export function lineBlocker(item: LineItem, byId: Map<string, LineItem>): LineItem | null {
  if (!item.on?.length) return null
  for (const number of item.on) {
    for (const other of byId.values()) {
      if (other.number === number && other.repo === item.repo && other.id !== item.id) return other
    }
  }
  return null
}
