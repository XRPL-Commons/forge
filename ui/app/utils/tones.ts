// The one place a state picks its colour. A value line uses a text tone; a
// badge uses the matching UBadge colour. Words always carry the meaning, so
// colour only reinforces it.
import type { IssueStatus } from './issues'

type Tone = 'ok' | 'warn' | 'muted'
export type BadgeColor = 'primary' | 'warning' | 'info' | 'neutral'

export const TONE_TEXT: Record<Tone, string> = { ok: 'text-white', warn: 'text-amber-200', muted: 'text-muted' }
export const TONE_BADGE: Record<Tone, BadgeColor> = { ok: 'primary', warn: 'warning', muted: 'neutral' }

const STATUS_TONES: Record<IssueStatus, BadgeColor> = {
  attention: 'warning', working: 'primary', completed: 'primary', queued: 'info', waiting: 'neutral', backlog: 'neutral',
  // Waiting on other issues is calm, never amber: amber means "needs you".
  blocked: 'neutral',
  // No longer needed: grey and quiet, like any work that left the line.
  dropped: 'neutral',
}

export function statusTone(status: IssueStatus): BadgeColor {
  return STATUS_TONES[status] ?? 'neutral'
}

// A status chip: its colour, an icon when the colour is shared with another
// status (so the chip never relies on colour alone), and a dim slate finish
// for the statuses that only wait on something else or left the line.
interface StatusBadge { color: BadgeColor; icon: string | null; dim: boolean }

const STATUS_ICONS: Partial<Record<IssueStatus, string>> = { blocked: 'i-lucide-link-2', dropped: 'i-lucide-archive' }

export function statusBadge(status: IssueStatus): StatusBadge {
  return { color: statusTone(status), icon: STATUS_ICONS[status] ?? null, dim: status === 'blocked' || status === 'dropped' }
}
