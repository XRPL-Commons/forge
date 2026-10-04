// The little character above each bay of the factory line's crate, in the
// strip's ink-outline style. One drawing per reason, as SVG path data on a
// 24x24 grid, so the canvas (through Path2D), the modal's filter chips and
// the hover cards all draw the very same thing.
import type { AttentionReason } from './factoryLine'

export const GLYPH_INK = '#0a0c0f'

export interface GlyphPart {
  d: string
  fill?: string
  // Outline colour; defaults to the ink. 'none' leaves the part unstroked.
  stroke?: string
  width?: number
  // Parts that move on their own: the "?" bounces, the bulb pops, the stamp
  // hovers, the arrow turns, the wrench nods, the lens peers.
  moves?: boolean
}

export const BAY_GLYPHS: Record<AttentionReason, GlyphPart[]> = {
  // A crumpled blueprint with a lightbulb popping out of it.
  approach: [
    { d: 'M3 14.2 7.5 13l3 1.4 4-1.6 3 1 3.2-.8-.6 8.2-3.4-.6-3.6 1.1-3.2-1.2L4 21.6Z', fill: '#4f7fd6' },
    { d: 'M6 16.8h5.5M7 19.2h8.5', stroke: '#c9dcff', width: 1.1 },
    { d: 'M12 1.8a4.2 4.2 0 0 1 2.5 7.6v1.4h-5V9.4A4.2 4.2 0 0 1 12 1.8Z', fill: '#ffd27a', moves: true },
    { d: 'M9.8 11.4h4.4', stroke: '#8a8f98', width: 1.8, moves: true },
  ],
  // A speech bubble with a bouncing question mark.
  question: [
    { d: 'M3 6a3 3 0 0 1 3-3h12a3 3 0 0 1 3 3v7.5a3 3 0 0 1-3 3h-6.5L7 20.5v-4H6a3 3 0 0 1-3-3Z', fill: '#edf1ee' },
    { d: 'M9.7 7.6a2.4 2.4 0 1 1 3.4 2.2c-.8.4-1.1.9-1.1 1.7', fill: 'none', width: 1.8, moves: true },
    { d: 'M12 13.1a1 1 0 1 1 0 2 1 1 0 0 1 0-2Z', fill: GLYPH_INK, stroke: 'none', moves: true },
  ],
  // A rubber stamp, hovering over the page.
  approve: [
    { d: 'M5 20.5h14', stroke: '#7fd6a0', width: 2.2 },
    { d: 'M12 1.8a2.8 2.8 0 1 1 0 5.6 2.8 2.8 0 0 1 0-5.6Z', fill: '#b07a45', moves: true },
    { d: 'M10.6 7.2h2.8v4h-2.8Z', fill: '#b07a45', moves: true },
    { d: 'M5.5 11.2h13a1 1 0 0 1 1 1v3.3h-15v-3.3a1 1 0 0 1 1-1Z', fill: '#7fd6a0', moves: true },
  ],
  // A small smoke puff and a circular arrow: try again.
  failed: [
    { d: 'M4.6 3.6a2 2 0 0 1 3.4-.4 1.8 1.8 0 0 1 1.6 3 1.7 1.7 0 0 1-2.6 1.1 1.9 1.9 0 0 1-2.4-3.7Z', fill: '#a3aab4' },
    { d: 'M18.6 12.6a6.6 6.6 0 1 1-2-5.3', fill: 'none', stroke: '#f0b44c', width: 2.4, moves: true },
    { d: 'M19.6 4.4v5h-5Z', fill: '#f0b44c', moves: true },
  ],
  // A wrench.
  env: [
    { d: 'M15 2.6a4.6 4.6 0 0 0-4.4 6.1l-7.2 7.2a1.9 1.9 0 0 0 2.7 2.7l7.2-7.2a4.6 4.6 0 0 0 6.1-4.4l-2.8 2.8-2.7-.6-.6-2.7Z', fill: '#c9d0d8', moves: true },
    { d: 'M18 19.5c1 .4 1.6 1.2 1.8 2.2M20.6 17.6c.9.2 1.6.8 2 1.6', fill: 'none', stroke: '#a3aab4', width: 1.2 },
  ],
  // A clipboard with a checkmark.
  verify: [
    { d: 'M5.5 4.5h13a1 1 0 0 1 1 1v15a1 1 0 0 1-1 1h-13a1 1 0 0 1-1-1v-15a1 1 0 0 1 1-1Z', fill: '#e4d3b0' },
    { d: 'M9 2.6h6v3.6H9Z', fill: '#a3aab4' },
    { d: 'M8 13.6l2.8 2.8 5.4-6', fill: 'none', stroke: '#2f9e64', width: 2.2, moves: true },
  ],
  // A magnifier with a pause sign in the lens.
  review: [
    { d: 'M14.6 14.6 20.6 20.6', stroke: '#a3aab4', width: 3.2 },
    { d: 'M10 3a6.6 6.6 0 1 1 0 13.2A6.6 6.6 0 0 1 10 3Z', fill: '#9cbaf4', moves: true },
    { d: 'M8.3 7.4v4.4M11.7 7.4v4.4', fill: 'none', width: 1.8, moves: true },
  ],
}

// How the moving parts move at time t (seconds): a gentle offset in glyph
// units. Still when motion is reduced.
export function glyphMotion(reason: AttentionReason, t: number, still: boolean): { dx: number; dy: number; turn: number } {
  if (still) return { dx: 0, dy: 0, turn: 0 }
  const wave = Math.sin(t * 2.2)
  switch (reason) {
    case 'question': return { dx: 0, dy: -Math.abs(Math.sin(t * 3)) * 1.6, turn: 0 }
    case 'approach': return { dx: 0, dy: -Math.max(0, wave) * 1.4, turn: 0 }
    case 'approve': return { dx: 0, dy: -1.2 - wave * 1.2, turn: 0 }
    case 'failed': return { dx: 0, dy: 0, turn: t * 1.4 }
    case 'env': return { dx: 0, dy: 0, turn: Math.sin(t * 2.6) * 0.18 }
    case 'verify': return { dx: 0, dy: -Math.max(0, Math.sin(t * 1.8)) * 0.8, turn: 0 }
    default: return { dx: Math.sin(t * 1.6) * 0.8, dy: Math.cos(t * 1.6) * 0.5, turn: 0 }
  }
}
