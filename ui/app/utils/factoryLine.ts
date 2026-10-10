// Pure model behind the factory line strip at the top of the Activity page
// (components/FactoryLine.vue). The strip draws the same issues the board
// groups, one cube per issue, so everything here is derived from
// groupIssues() and the raw ledger fields it does not carry. Motion is never
// simulated: committed event frames authorize movement between projected positions.
import {
  COLUMN_ROLES, NEXT_ROLE, groupIssues, mergedToday, workerUsage, type WorkerUsage,
  type CardModel, type ColumnRole, type FactoryView, type IssueGroups, type LedgerIssueView,
} from './factory'

import type { FactoryTransition } from './factoryStore'

/** Align motion to the committed event, using server age to avoid client clock skew. */
export function lineTransition(frame: FactoryTransition | null | undefined, cursor: string | undefined, consumed: string | null, now: number) {
  if (!frame || frame.cursor !== cursor || frame.cursor === consumed) return null
  const latest = frame.events.at(-1)
  const delay = Math.max(0, now - frame.receivedAt)
  const age = latest ? Math.max(0, (frame.at - latest.at) * 1000) + delay : Infinity
  const oldestAge = latest ? Math.max(0, (frame.at - Math.min(...frame.events.map(event => event.at))) * 1000) + delay : Infinity
  return { cursor: frame.cursor, animate: !frame.resync && oldestAge < 1500, at: now - age }
}

export type LineStage = 'queued' | ColumnRole | 'approval' | 'merged' | 'attention' | 'waiting' | 'hold'
// Why an issue waits for the operator: the server's `attention_kind` code.
export type AttentionReason = 'approach' | 'question' | 'approve' | 'failed' | 'env' | 'review' | 'verify'

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
  // At a machine but not running: its job is reserved and not yet launched,
  // or the stage is checking the job that just finished (#241).
  phase?: 'starting' | 'checking'
  // The plain-language second line of the tooltip.
  status: string
  card: CardModel
}

export interface LineState {
  usage: WorkerUsage
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
  // Leased worker slots, and how many have fresh running observations.
  occupied: number
  running: number
  slots: number
  attention: number
}
export interface HeadlinePart { t: string; c: 'amber' | 'coral' | 'text' | 'muted' }

export const LINE_STAGES: LineStage[] = ['queued', 'build', 'review', 'audit', 'approval', 'merged', 'attention', 'waiting', 'hold']
export const LINE_REASONS: AttentionReason[] = ['approach', 'question', 'approve', 'verify', 'failed', 'env', 'review']
export const LINE_STAGE_LABELS: Record<LineStage, string> = {
  queued: 'Queued', build: 'Build', review: 'Review', audit: 'Security audit', approval: 'Waiting to merge',
  merged: 'Merged', attention: 'Needs attention', waiting: 'Waiting on other issues', hold: 'On hold',
}
export const LINE_SHORT_LABELS: Record<ColumnRole, string> = { build: 'Build', review: 'Review', audit: 'Audit' }
// The bay label under the crate, and the plain sentence on cards and filters.
export const LINE_REASON_SHORT: Record<AttentionReason, string> = {
  approach: 'new approach', question: 'question', approve: 'approve', failed: 'retry', env: 'fix setup', review: 'review', verify: 'verify',
}
export const LINE_REASON_TEXT: Record<AttentionReason, string> = {
  approach: 'Out of attempts: describe a different approach, then retry',
  question: 'The worker asked you something',
  approve: 'A pull request waits for your approval',
  failed: 'An attempt failed',
  env: "Forge's setup needs repair",
  review: 'Paused or changed while being applied',
  verify: "A person needs to check something workers can't",
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

// The server says why an issue waits for the operator; an issue without a
// code is not waiting on them (a deliberate pause sleeps on the shelf).
export function lineReason(issue: Pick<LedgerIssueView, 'attention_kind'>): AttentionReason | null {
  const kind = issue.attention_kind
  return kind && (LINE_REASONS as string[]).includes(kind) ? kind as AttentionReason : null
}

const STARTING_PHASES = new Set(['reserved', 'created', 'launching'])

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
      ...(card.operator_status ? { status: card.operator_status.label } : {}),
    })
  }
  const jobs = new Map((view.jobs ?? []).map(job => [job.id, job]))
  for (const role of COLUMN_ROLES) {
    for (const card of groups.columns[role]) {
      const stage: LineStage = card.jobId || card.checking ? role : 'queued'
      const job = card.jobId ? jobs.get(card.jobId) : undefined
      const phase = card.checking ? 'checking' : job && STARTING_PHASES.has(job.phase) ? 'starting' : undefined
      add(card, stage, { next: stage === 'queued' ? role : undefined, status: columnStatus(card, stage), ...(phase ? { phase } : {}) })
    }
  }
  for (const card of groups.finishing) add(card, 'approval', { status: card.phaseLabel === 'Merging' ? 'Merging' : 'Approved, waiting to merge' })
  for (const card of [...groups.attention, ...groups.held]) {
    const issue = byKey.get(card.key)
    const reason = issue ? lineReason(issue) : null
    if (reason) add(card, 'attention', { reason, status: LINE_REASON_TEXT[reason] })
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
    items, usage: workerUsage(view, now),
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
    if (waiting > 0 && state.usage.roles[stage].occupied >= state.slots[stage]) necks.push({ stage, waiting })
  }
  necks.sort((a, b) => b.waiting - a.waiting)
  const reasons = Object.fromEntries(LINE_REASONS.map(reason => [reason, 0])) as Record<AttentionReason, number>
  for (const item of groups.attention) reasons[item.reason!] += 1
  return {
    necks, neck: necks[0] ?? null, reasons,
    occupied: state.usage.occupied,
    running: state.usage.running,
    slots: COLUMN_ROLES.reduce((total, stage) => total + state.slots[stage], 0),
    attention: groups.attention.length,
  }
}

// `account` names a coding account that is down ("Codex signed out"); it
// leads, because nothing else on the line moves while it is.
export function lineHeadline(info: LineInfo, account?: string | null): HeadlinePart[] {
  const parts: HeadlinePart[] = []
  if (account) parts.push({ t: account, c: 'coral' })
  if (info.attention) parts.push({ t: `${info.attention} need${info.attention === 1 ? 's' : ''} you`, c: 'amber' })
  if (info.neck) parts.push({ t: `${LINE_SHORT_LABELS[info.neck.stage]} is the bottleneck, ${info.neck.waiting} waiting`, c: 'coral' })
  else if (info.running) parts.push({ t: `${info.running} of ${info.slots} slots working`, c: 'text' })
  else if (info.occupied) parts.push({ t: `${info.occupied} of ${info.slots} slots in use`, c: 'text' })
  else if (!info.attention) parts.push({ t: 'All quiet', c: 'muted' })
  return parts
}

export function lineHeadlineText(parts: HeadlinePart[]): string {
  return parts.map(part => part.t).join(' · ')
}

// One sentence for screen readers: the counts the strip draws.
export function lineSummary(state: LineState, info: LineInfo): string {
  const groups = lineGroups(state.items)
  const stage = (role: ColumnRole) => `${LINE_SHORT_LABELS[role]} ${state.usage.roles[role].occupied} of ${state.slots[role]}`
  return [
    `Queued ${groups.queued.length}`, ...COLUMN_ROLES.map(stage),
    `waiting to merge ${groups.approval.length}`, `merged today ${state.merged}`,
    `need you ${info.attention}`, `waiting on other issues ${groups.waiting.length}`, `on hold ${groups.hold.length}`,
  ].join(', ') + '.'
}

export function lineTooltip(item: LineItem, showRepo: boolean): string {
  const name = item.number != null ? `#${item.number}` : item.id
  const canonical = item.card.operator_status
  const label = canonical?.label ?? LINE_STAGE_LABELS[item.stage]
  const reason = canonical?.reason ?? item.status
  const parts = [name, label]
  if (item.phase === 'starting') parts.push('Starting')
  if (reason && reason !== label) parts.push(reason)
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
