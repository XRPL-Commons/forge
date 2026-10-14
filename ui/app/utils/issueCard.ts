// What one IssueCard shows, from either shape a page holds: an Issues page
// row (/api/issues) or an Activity card (/api/factory). The card itself only
// renders this view, so both pages read the same way.
import { operatorReason } from './operatorStatus'
import { elapsed, type CardModel } from './factory'
import { ago } from './format'
import { rowFacts, rowStory, statusLabel, type IssueRow } from './issues'
import { statusBadge, type BadgeColor } from './tones'
import { portraitFromScene, type PortraitLook } from './cube'
import { workbenchLook } from './workbench'

interface CardHearts { lost: number; limit: number }
export interface CardFact {
  key: string
  text: string
  tone: 'link' | 'warn' | 'ok' | 'muted'
  // A fact with a destination is its own link inside the card.
  to?: string
  hearts?: CardHearts
}
export interface IssueCardView {
  key: string
  id: string
  to: string
  repo: string
  number: number | null
  title: string
  // One summary, clamped: Forge's headline, then the plain sentences behind it.
  headline: string
  plain: string
  facts: CardFact[]
  statusReason?: string
  // `icon` and `dim`: the calm chip of an issue waiting on other issues.
  status: { label: string; color: BadgeColor; icon?: string | null; dim?: boolean } | null
  look: PortraitLook
  running: boolean
}

// Hearts only read when there are few of them; a bigger limit stays text.
function hearts(lost: number, limit: number): CardHearts | undefined {
  return limit > 0 && limit <= 5 && lost > 0 ? { lost: Math.min(lost, limit), limit } : undefined
}

export function rowCardView(row: IssueRow, now: number): IssueCardView {
  const story = rowStory(row)
  const facts = rowFacts(row, now).map((fact): CardFact => {
    if (fact.key.startsWith('pr-')) return { ...fact, tone: 'link' }
    if (fact.key === 'attempts') return { ...fact, tone: 'warn', hearts: hearts(row.attempts?.failed ?? 0, row.attempts?.limit ?? 0) }
    if (fact.key === 'hold') return { ...fact, tone: 'warn' }
    return { ...fact, tone: 'muted' }
  })
  return {
    key: row.key,
    id: `issue-${row.repo_id}-${row.number}`,
    to: `/issues/${encodeURIComponent(row.key)}`,
    repo: row.repo,
    number: row.number,
    title: row.title,
    headline: story.headline,
    plain: story.plain,
    facts,
    statusReason: operatorReason(row),
    status: { label: statusLabel(row), ...statusBadge(row.status) },
    look: portraitFromScene(workbenchLook(row).scene),
    running: row.status === 'working',
  }
}

const ATTEMPTS = /^(\d+) \/ (\d+) attempts failed$/

function cardColor(card: CardModel): BadgeColor {
  if (card.state === 'failed' || card.state === 'approval_needed' || card.state === 'hold') return 'warning'
  if (card.state === 'merged' || card.running) return 'primary'
  if (card.state === 'cancelled') return 'neutral'
  return 'info'
}

function cardAge(card: CardModel, now: number): string {
  if (!now) return ''
  if (card.doneAt) return `${card.state === 'cancelled' ? 'Closed' : 'Merged'} ${ago(now - card.doneAt)}`
  return card.updatedAt ? `Updated ${ago(now - card.updatedAt)}` : ''
}

export function modelCardView(card: CardModel, now: number): IssueCardView {
  const facts: CardFact[] = []
  const timer = card.timerStart !== null && card.timerEnd !== null ? elapsed(card.timerEnd - card.timerStart) : ''
  const progress = [card.providerLine, timer].filter(Boolean).join(' · ')
  if (progress) facts.push({ key: 'progress', text: progress, tone: card.running ? 'ok' : 'muted' })
  if (card.pr) facts.push({ key: `pr-${card.pr}`, text: `PR #${card.pr}`, tone: 'link' })
  const attempts = card.footerLink && !card.footerLink.to ? ATTEMPTS.exec(card.footerLink.label) : null
  if (attempts) {
    const lost = Number(attempts[1]), limit = Number(attempts[2])
    facts.push({ key: 'attempts', text: `${lost} of ${limit} attempts failed`, tone: 'warn', hearts: hearts(lost, limit) })
  } else if (card.footerLink) {
    facts.push({ key: 'note', text: card.footerLink.label, tone: card.footerLink.to ? 'link' : 'warn', to: card.footerLink.to || undefined })
  }
  if (card.deployment) facts.push({ key: 'deployment', text: card.deployment, tone: card.deployment === 'deploy failed' ? 'warn' : 'ok' })
  const age = cardAge(card, now)
  if (age) facts.push({ key: 'age', text: age, tone: 'muted' })
  const headline = card.summary || card.reason
  return {
    key: card.key,
    id: card.id,
    to: card.to,
    repo: card.repo,
    number: card.number,
    title: card.title,
    headline,
    plain: card.summary && card.reason && card.reason !== card.summary ? card.reason : '',
    facts,
    statusReason: operatorReason(card),
    status: card.operator_status ? { label: card.operator_status.label, ...statusBadge(card.operator_status.code) }
      : !card.phaseLabel ? null
      : card.look.mood === 'patient' ? { label: card.phaseLabel, ...statusBadge('blocked') }
        : card.look.mood === 'rest' ? { label: card.phaseLabel, ...statusBadge('dropped') }
        : { label: card.phaseLabel, color: cardColor(card) },
    look: card.look,
    running: card.running,
  }
}

// Recently completed, newest first, as the server pages it: by completion
// time, then key. A newer read of a card replaces the older one.
export function mergeCompleted(have: CardModel[], incoming: CardModel[]): CardModel[] {
  const byKey = new Map(have.map(card => [card.key, card]))
  for (const card of incoming) byKey.set(card.key, card)
  return [...byKey.values()].sort((a, b) => (b.doneAt ?? 0) - (a.doneAt ?? 0) || (a.key < b.key ? 1 : a.key > b.key ? -1 : 0))
}

// The request for one page of Recently completed.
const COMPLETED_PAGE = 10
export function completedPath(before: string | null): string {
  return `/api/factory?section=completed&limit=${COMPLETED_PAGE}${before ? `&before=${encodeURIComponent(before)}` : ''}`
}
