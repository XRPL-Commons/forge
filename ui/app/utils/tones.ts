// The one place a state picks its colour. A value line uses a text tone; a
// badge uses the matching UBadge colour. Words always carry the meaning, so
// colour only reinforces it.
import type { IssueStatus } from './issues'

export type Tone = 'ok' | 'warn' | 'muted'
export type BadgeColor = 'primary' | 'warning' | 'info' | 'neutral'

export const TONE_TEXT: Record<Tone, string> = { ok: 'text-white', warn: 'text-amber-200', muted: 'text-muted' }
export const TONE_BADGE: Record<Tone, BadgeColor> = { ok: 'primary', warn: 'warning', muted: 'neutral' }

const STATUS_TONES: Record<IssueStatus, BadgeColor> = {
  attention: 'warning', working: 'primary', completed: 'primary', queued: 'info', waiting: 'neutral', backlog: 'neutral',
}

export function statusTone(status: IssueStatus): BadgeColor {
  return STATUS_TONES[status] ?? 'neutral'
}
