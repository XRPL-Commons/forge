// The cube character, shared by the factory line on the Activity page and the
// workbench at the top of the issue panel: one drawing, one palette. Pure
// canvas helpers — no Vue, no DOM lookups — so both components and their tests
// can use them. The stage colours are the factory line's (LINE_LOOKS); main.css
// carries the same values as --color-stage-* tokens.
import { LINE_LOOKS, LINE_RESERVED } from './factoryLine'
import type { StageId } from './pipeline'

export const CUBE_INK = '#0a0c0f'
export const CUBE_AMBER = LINE_RESERVED.amber
const TAU = Math.PI * 2
const SANS = 'system-ui, -apple-system, sans-serif'
const MONO = 'ui-monospace, "SF Mono", Menlo, monospace'

// A cube's colour at each stage of the pipeline.
export const STAGE_FILL: Record<StageId, string> = {
  queued: LINE_LOOKS.queued.fill,
  build: LINE_LOOKS.build.fill,
  review: LINE_LOOKS.review.fill,
  audit: LINE_LOOKS.audit.fill,
  approval: LINE_LOOKS.approval.fill,
  merged: LINE_LOOKS.merged.fill,
}

// `puzzled`: one brow up; `grumpy`: brows knitted down toward the middle.
export type CubeEyes = 'open' | 'closed' | 'worried' | 'happy' | 'puzzled' | 'grumpy' | 'none'
export type CubeMouth = 'smile' | 'flat' | 'wobble' | 'o'
// Earned along the way: a hard hat while building, a magnifier in review, a
// rosette once the security review passed, a bow when merged.
export type CubeAccessory = 'hat' | 'magnifier' | 'badge' | 'bow'
// `patient`: waiting on other issues, eyes on the cubes it is tied to.
// `rest`: no longer needed: eyes closed, no z's, perfectly still.
export type CubeMood = 'busy' | 'worried' | 'hopeful' | 'sleep' | 'happy' | 'open' | 'patient' | 'rest'

export interface CubeOptions {
  alpha?: number; sx?: number; sy?: number
  glow?: string | null; glowBlur?: number
  look?: number; lookY?: number; eyeCol?: string; eyes?: CubeEyes
  // A blink closes open eyes for a frame.
  blink?: boolean
  // Drawn only on cubes big enough to read it (22px and up).
  mouth?: CubeMouth | null
  accessory?: CubeAccessory | null
  // A sticking plaster on the corner, and soot smudges after a failed run.
  bandage?: boolean
  soot?: boolean
}

export function roundRect(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number) {
  r = Math.max(0, Math.min(r, w / 2, h / 2))
  ctx.beginPath(); ctx.moveTo(x + r, y)
  ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r)
  ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath()
}

// The cube, standing on (x, y) with side c.
export function drawCube(ctx: CanvasRenderingContext2D, x: number, y: number, c: number, fill: string, o: CubeOptions = {}) {
  ctx.save(); ctx.globalAlpha = o.alpha == null ? 1 : o.alpha
  ctx.translate(x, y); ctx.scale(o.sx || 1, o.sy || 1)
  const lw = Math.max(1.6, c * 0.035)
  if (o.glow) { ctx.shadowColor = o.glow; ctx.shadowBlur = o.glowBlur || 10 }
  roundRect(ctx, -c / 2, -c, c, c, c * 0.24); ctx.fillStyle = fill; ctx.fill(); ctx.shadowBlur = 0
  ctx.lineWidth = lw; ctx.strokeStyle = CUBE_INK; ctx.stroke()
  const inset = lw + 0.9
  roundRect(ctx, -c / 2 + inset, -c + inset, c - 2 * inset, c * 0.26, c * 0.12); ctx.fillStyle = 'rgba(255,255,255,.22)'; ctx.fill()
  drawFace(ctx, c, o)
  if (o.soot) {
    ctx.fillStyle = 'rgba(16,14,12,.55)'
    for (const [px, py, pr] of [[-0.28, -0.14, 0.07], [-0.12, -0.08, 0.05], [0.24, -0.2, 0.06]] as const) { ctx.beginPath(); ctx.arc(c * px, c * py, c * pr, 0, TAU); ctx.fill() }
  }
  if (o.bandage) {
    ctx.save(); ctx.translate(c * 0.24, -c * 0.8); ctx.rotate(-0.7)
    roundRect(ctx, -c * 0.2, -c * 0.07, c * 0.4, c * 0.14, c * 0.05); ctx.fillStyle = '#efe3c8'; ctx.fill()
    ctx.lineWidth = Math.max(0.8, c * 0.03); ctx.strokeStyle = CUBE_INK; ctx.stroke()
    ctx.fillStyle = 'rgba(10,12,15,.45)'; ctx.fillRect(-c * 0.05, -c * 0.05, c * 0.1, c * 0.1)
    ctx.restore()
  }
  if (o.accessory) drawAccessory(ctx, o.accessory, c, lw)
  ctx.restore()
}

// Eyes, brows and mouth, in the coordinates of a cube of side c standing on
// (0, 0): the issue cube and the agent worker share one face.
export function drawFace(ctx: CanvasRenderingContext2D, c: number, o: Pick<CubeOptions, 'eyes' | 'look' | 'lookY' | 'eyeCol' | 'blink' | 'mouth'> = {}, mouthFrom = 22) {
  const ex = c * 0.19, ey = -c * 0.46, er = Math.max(1.2, c * 0.075), look = o.look || 0, lookY = o.lookY || 0
  const ink = o.eyeCol || '#12151a'
  ctx.fillStyle = ink; ctx.strokeStyle = ink; ctx.lineWidth = Math.max(1, c * 0.07); ctx.lineCap = 'round'
  const eyes = o.eyes ?? 'open'
  const shut = eyes === 'closed' || (o.blink && (eyes === 'open' || eyes === 'worried'))
  if (shut) {
    ctx.beginPath()
    ctx.moveTo(-ex - er * 1.4, ey + 1); ctx.lineTo(-ex + er * 1.4, ey + 1)
    ctx.moveTo(ex - er * 1.4, ey + 1); ctx.lineTo(ex + er * 1.4, ey + 1)
    ctx.stroke()
  } else if (eyes === 'happy') {
    for (const side of [-1, 1]) { ctx.beginPath(); ctx.arc(side * ex, ey + er * 0.9, er * 1.35, Math.PI * 1.15, Math.PI * 1.85); ctx.stroke() }
  } else if (eyes !== 'none') {
    ctx.beginPath(); ctx.arc(-ex + look, ey + lookY, er, 0, TAU); ctx.arc(ex + look, ey + lookY, er, 0, TAU); ctx.fill()
  }
  if (eyes === 'worried') {
    ctx.beginPath()
    ctx.moveTo(-ex - er * 1.8, ey - er * 2.2); ctx.lineTo(-ex + er * 1.2, ey - er * 3.4)
    ctx.moveTo(ex + er * 1.8, ey - er * 2.2); ctx.lineTo(ex - er * 1.2, ey - er * 3.4)
    ctx.stroke()
  }
  if (eyes === 'puzzled' || eyes === 'grumpy') {
    ctx.beginPath()
    if (eyes === 'puzzled') {
      ctx.moveTo(-ex - er * 1.5, ey - er * 2.6); ctx.lineTo(-ex + er * 1.3, ey - er * 3.6)
      ctx.moveTo(ex - er * 1.4, ey - er * 2.3); ctx.lineTo(ex + er * 1.4, ey - er * 2.3)
    } else {
      ctx.moveTo(-ex - er * 1.6, ey - er * 3.3); ctx.lineTo(-ex + er * 1.4, ey - er * 2.1)
      ctx.moveTo(ex + er * 1.6, ey - er * 3.3); ctx.lineTo(ex - er * 1.4, ey - er * 2.1)
    }
    ctx.stroke()
  }
  if (c >= mouthFrom && o.mouth) {
    const my = -c * 0.25, mx = look * 0.5
    ctx.lineWidth = Math.max(1, c * 0.045); ctx.beginPath()
    if (o.mouth === 'smile') ctx.arc(mx, my - c * 0.06, c * 0.09, Math.PI * 0.2, Math.PI * 0.8)
    else if (o.mouth === 'flat') { ctx.moveTo(mx - c * 0.06, my); ctx.lineTo(mx + c * 0.06, my) }
    else if (o.mouth === 'wobble') {
      ctx.moveTo(-c * 0.09, my); ctx.quadraticCurveTo(-c * 0.045, my - c * 0.045, 0, my); ctx.quadraticCurveTo(c * 0.045, my + c * 0.045, c * 0.09, my)
    } else ctx.arc(0, my, c * 0.035, 0, TAU)
    ctx.stroke()
  }
}

// Drawn in the cube's own coordinates: (0, 0) is the middle of its base.
export function drawAccessory(ctx: CanvasRenderingContext2D, kind: CubeAccessory, c: number, lw: number) {
  ctx.lineWidth = lw; ctx.strokeStyle = CUBE_INK; ctx.lineCap = 'round'
  if (kind === 'hat') {
    const hy = -c + c * 0.03
    ctx.beginPath(); ctx.arc(0, hy, c * 0.34, Math.PI, 0); ctx.closePath(); ctx.fillStyle = '#eef1f4'; ctx.fill(); ctx.stroke()
    ctx.strokeStyle = '#b6bec8'; ctx.lineWidth = Math.max(1, c * 0.04)
    ctx.beginPath(); ctx.moveTo(0, hy - c * 0.31); ctx.lineTo(0, hy - 1); ctx.stroke()
    ctx.strokeStyle = CUBE_INK; ctx.lineWidth = lw
    roundRect(ctx, -c * 0.5, hy - c * 0.05, c, c * 0.1, c * 0.05); ctx.fillStyle = '#eef1f4'; ctx.fill(); ctx.stroke()
  } else if (kind === 'magnifier') {
    const lx = c * 0.52, ly = -c * 0.5, r = c * 0.19
    ctx.lineWidth = c * 0.1; ctx.beginPath(); ctx.moveTo(lx + r * 0.7, ly + r * 0.7); ctx.lineTo(lx + r * 1.8, ly + r * 1.9); ctx.stroke()
    ctx.lineWidth = c * 0.06; ctx.strokeStyle = '#8a6a4c'; ctx.stroke()
    ctx.beginPath(); ctx.arc(lx, ly, r, 0, TAU); ctx.fillStyle = 'rgba(214,234,255,.3)'; ctx.fill()
    ctx.lineWidth = c * 0.085; ctx.strokeStyle = CUBE_INK; ctx.stroke(); ctx.lineWidth = c * 0.045; ctx.strokeStyle = '#d5dbe2'; ctx.stroke()
    ctx.beginPath(); ctx.arc(lx - r * 0.3, ly - r * 0.3, r * 0.28, Math.PI, Math.PI * 1.5)
    ctx.strokeStyle = 'rgba(255,255,255,.7)'; ctx.lineWidth = Math.max(1, c * 0.03); ctx.stroke()
  } else if (kind === 'badge') {
    const bx = -c * 0.27, by = -c * 0.21, r = c * 0.1
    ctx.fillStyle = '#7a62c9'
    for (const side of [-1, 1]) {
      ctx.beginPath(); ctx.moveTo(bx + side * r * 0.7, by); ctx.lineTo(bx + side * r * 0.9, by + r * 2)
      ctx.lineTo(bx + side * r * 0.3, by + r * 1.6); ctx.lineTo(bx, by + r * 0.2); ctx.closePath(); ctx.fill()
    }
    ctx.beginPath(); ctx.arc(bx, by, r, 0, TAU); ctx.fillStyle = '#c9b8ff'; ctx.fill(); ctx.lineWidth = Math.max(1.2, lw * 0.8); ctx.stroke()
    ctx.beginPath(); ctx.moveTo(bx - r * 0.45, by); ctx.lineTo(bx - r * 0.1, by + r * 0.4); ctx.lineTo(bx + r * 0.5, by - r * 0.35)
    ctx.lineWidth = Math.max(1, c * 0.03); ctx.stroke()
  } else {
    ctx.save(); ctx.translate(-c * 0.3, -c * 0.97); ctx.rotate(-0.3)
    const b = c * 0.2
    ctx.fillStyle = '#f08bb0'; ctx.lineWidth = Math.max(1.2, lw * 0.85)
    for (const side of [-1, 1]) {
      ctx.beginPath(); ctx.moveTo(0, 0); ctx.lineTo(side * b, -b * 0.6); ctx.lineTo(side * b, b * 0.6); ctx.closePath(); ctx.fill(); ctx.stroke()
    }
    ctx.beginPath(); ctx.arc(0, 0, b * 0.3, 0, TAU); ctx.fillStyle = '#f6b3cb'; ctx.fill(); ctx.stroke()
    ctx.restore()
  }
}

// How a mood sets the face.
export function moodFace(mood: CubeMood, c: number): Pick<CubeOptions, 'eyes' | 'look' | 'lookY' | 'mouth'> {
  switch (mood) {
    case 'busy': return { eyes: 'open', look: 0, lookY: c * 0.07, mouth: 'flat' }
    case 'worried': return { eyes: 'worried', look: 0, lookY: -c * 0.02, mouth: 'wobble' }
    case 'hopeful': return { eyes: 'open', look: c * 0.08, lookY: -c * 0.05, mouth: 'smile' }
    case 'happy': return { eyes: 'happy', mouth: 'smile' }
    case 'sleep': return { eyes: 'closed', mouth: 'o' }
    case 'rest': return { eyes: 'closed', mouth: null }
    case 'patient': return { eyes: 'open', look: -c * 0.09, lookY: c * 0.02, mouth: 'flat' }
    default: return { eyes: 'open', look: c * 0.07, mouth: null }
  }
}

export interface Motion { dx: number; dy: number; sx: number; sy: number; blink: boolean }
// Idle motion at time t (ms): a hop that started at `hop`, then a blink every
// few seconds and a small per-mood bob. `still` gives the resting pose.
export function cubeMotion(t: number, hop: number, seed: number, c: number, mood: CubeMood, still: boolean): Motion {
  let dx = 0, dy = 0, sx = 1, sy = 1
  if (still) return { dx, dy, sx, sy, blink: false }
  const k = (t - hop) / 560
  if (k >= 0 && k < 1) { dy -= Math.sin(Math.PI * k) * c * 0.7; sy = 1 + 0.12 * Math.sin(Math.PI * k); sx = 1 / sy }
  else if (k >= 1 && k < 1.45) { const q = 0.18 * (1 - (k - 1) / 0.45); sy = 1 - q; sx = 1 + q * 0.8 }
  else {
    if (mood === 'busy') dx = Math.sin(t / 55) * 0.45
    if (mood === 'hopeful' || mood === 'happy') dy = -Math.max(0, Math.sin(t / 380 + seed)) * 2
    if (mood === 'worried') dy = -Math.pow(Math.max(0, Math.sin(t / 520 + seed)), 10) * 2.5
    if (mood === 'sleep') { sy = 1 + Math.sin(t / 900) * 0.025; sx = 1 / sy }
    if (mood === 'open') dy = -Math.max(0, Math.sin(t / 900 + seed)) * 1
    if (mood === 'patient') dx = Math.sin(t / 1600 + seed) * 0.8
  }
  return { dx, dy, sx, sy, blink: ((t + seed * 997) % 4300) < 130 }
}

// ---------------------------------------------------------------------------
// Props around the character. `still` draws the reduced-motion frame.
// ---------------------------------------------------------------------------
export function drawScorch(ctx: CanvasRenderingContext2D, x: number, y: number, r: number, seed: number) {
  ctx.beginPath()
  for (let i = 0; i < 12; i++) {
    const a = i / 12 * TAU, k = (i % 2 ? 0.5 : 1) * (0.8 + 0.35 * Math.abs(Math.sin(seed * 7.1 + i * 2.3)))
    const px = x + Math.cos(a) * r * k * 1.25, py = y + Math.sin(a) * r * k * 0.8
    if (i) ctx.lineTo(px, py); else ctx.moveTo(px, py)
  }
  ctx.closePath(); ctx.fillStyle = 'rgba(8,8,9,.82)'; ctx.fill()
  ctx.beginPath(); ctx.ellipse(x, y, r * 0.45, r * 0.3, 0, 0, TAU); ctx.fillStyle = '#3a2c25'; ctx.fill()
}

export function drawSmoke(ctx: CanvasRenderingContext2D, x: number, y: number, s: number, t: number, still: boolean, col = '#7d858f') {
  for (let i = 0; i < 3; i++) {
    const ph = still ? i / 3 + 0.15 : ((t / 1600) + i / 3) % 1
    ctx.globalAlpha = (1 - ph) * 0.55; ctx.beginPath()
    ctx.arc(x + ph * 7 * s, y - ph * 18 * s, (2 + ph * 3.5) * s, 0, TAU); ctx.fillStyle = col; ctx.fill()
  }
  ctx.globalAlpha = 1
}

export function drawZzz(ctx: CanvasRenderingContext2D, x: number, y: number, s: number, t: number, still: boolean) {
  ctx.fillStyle = '#8b929c'; ctx.textAlign = 'left'
  for (let i = 0; i < 2; i++) {
    const ph = still ? 0.35 + i * 0.4 : ((t / 1900) + i * 0.5) % 1
    ctx.globalAlpha = Math.sin(ph * Math.PI) * 0.85; ctx.font = `600 ${(9 + ph * 5) * s}px ${SANS}`
    ctx.fillText('z', x + ph * 8 * s, y - ph * 16 * s)
  }
  ctx.globalAlpha = 1
}

export function drawAmberGlow(ctx: CanvasRenderingContext2D, x: number, y: number, r: number, t: number, still: boolean) {
  const p = still ? 0.5 : 0.5 + 0.5 * Math.sin(t / 420)
  const g = ctx.createRadialGradient(x, y, 2, x, y, r)
  g.addColorStop(0, `rgba(240,180,76,${0.16 + 0.1 * p})`); g.addColorStop(1, 'rgba(240,180,76,0)')
  ctx.fillStyle = g; ctx.fillRect(x - r, y - r, r * 2, r * 2)
}

// The amber "needs you" light, standing on (x, y).
export function drawBeacon(ctx: CanvasRenderingContext2D, x: number, y: number, s: number, t: number, still: boolean) {
  const pulse = still ? 1 : 0.5 + 0.5 * Math.sin(t / 300)
  if (!still) {
    const ang = t / 320
    ctx.save(); ctx.translate(x, y - 2 * s); ctx.fillStyle = 'rgba(240,180,76,.16)'
    for (const a0 of [ang, ang + Math.PI]) { ctx.beginPath(); ctx.moveTo(0, 0); ctx.arc(0, 0, 30 * s, a0 - 0.3, a0 + 0.3); ctx.closePath(); ctx.fill() }
    ctx.restore()
  }
  ctx.beginPath(); ctx.arc(x, y - 2 * s, 5 * s, Math.PI, 0); ctx.lineTo(x + 5 * s, y); ctx.lineTo(x - 5 * s, y); ctx.closePath()
  ctx.fillStyle = pulse > 0.5 ? '#ffd27a' : CUBE_AMBER; ctx.fill(); ctx.lineWidth = 1.4; ctx.strokeStyle = CUBE_INK; ctx.stroke()
  roundRect(ctx, x - 6.5 * s, y, 13 * s, 3.5 * s, 1.5); ctx.fillStyle = '#4a4f57'; ctx.fill(); ctx.stroke()
}

export function drawTether(ctx: CanvasRenderingContext2D, x1: number, y1: number, x2: number, y2: number, lift: number) {
  ctx.save(); ctx.setLineDash([2, 3]); ctx.strokeStyle = 'rgba(160,168,178,.7)'; ctx.lineWidth = 1.2
  ctx.beginPath(); ctx.moveTo(x1, y1); ctx.quadraticCurveTo((x1 + x2) / 2, Math.min(y1, y2) - lift, x2, y2); ctx.stroke(); ctx.restore()
}

// A dependency: a grey cube with its issue number above it.
export function drawGhost(ctx: CanvasRenderingContext2D, x: number, y: number, c: number, label: string) {
  drawCube(ctx, x, y, c, '#59606b', { eyes: 'open', look: c * 0.08, eyeCol: '#14171b', alpha: 0.85 })
  ctx.font = `500 10px ${MONO}`; ctx.fillStyle = '#b3b9c1'; ctx.textAlign = 'center'; ctx.fillText(label, x, y - c - 4); ctx.textAlign = 'left'
}

export function drawConfetti(ctx: CanvasRenderingContext2D, W: number, floor: number, t: number, still: boolean) {
  const cols = [STAGE_FILL.merged, STAGE_FILL.approval, STAGE_FILL.build, STAGE_FILL.audit, '#f08bb0']
  for (let i = 0; i < 14; i++) {
    const x = (i * 53.7 + 17) % (W - 20) + 10, speed = 18 + (i % 5) * 4
    const y = still ? (i * 37) % (floor - 20) + 8 : ((i * 37) + t / 1000 * speed) % (floor - 8)
    ctx.save(); ctx.translate(x, y); ctx.rotate(i + (still ? 0 : t / 900)); ctx.fillStyle = cols[i % 5]!; ctx.globalAlpha = 0.75
    ctx.fillRect(-2.5, -1.2, 5, 2.4); ctx.restore()
  }
}

// ---------------------------------------------------------------------------
// The workbench scene: one big cube on a bench, with its props.
// ---------------------------------------------------------------------------
export interface BenchScene {
  stage: StageId
  mood: CubeMood
  accessory: CubeAccessory | null
  scorches: number
  beacon: boolean
  zzz: boolean
  smoke: boolean
  toolbox: boolean
  dependencies: number[]
  confetti: boolean
}

export function drawBench(ctx: CanvasRenderingContext2D, W: number, H: number, scene: BenchScene, t: number, hop: number, seed: number, still: boolean) {
  const gy = H - 30
  ctx.fillStyle = '#16191f'; ctx.fillRect(0, 0, W, H)
  ctx.fillStyle = '#1c2027'
  for (let y = 10; y < gy - 4; y += 14) for (let x = 10 + ((y / 14) % 2) * 7; x < W - 4; x += 14) { ctx.beginPath(); ctx.arc(x, y, 1.1, 0, TAU); ctx.fill() }
  const deps = scene.dependencies.slice(0, 3)
  const c = Math.round(Math.min(76, W * 0.36, H * 0.46)), cx = deps.length ? W / 2 + Math.min(30, W * 0.1) : W / 2
  if (scene.beacon) drawAmberGlow(ctx, cx, gy - c * 0.5, c * 1.5, t, still)

  // The bench.
  ctx.lineWidth = 1.6; ctx.strokeStyle = CUBE_INK
  roundRect(ctx, 22, gy + 10, 7, H, 2); ctx.fillStyle = '#2b2f35'; ctx.fill(); ctx.stroke()
  roundRect(ctx, W - 29, gy + 10, 7, H, 2); ctx.fill(); ctx.stroke()
  roundRect(ctx, 10, gy, W - 20, 10, 3); ctx.fillStyle = '#3a3f47'; ctx.fill(); ctx.stroke()
  ctx.fillStyle = '#4a515b'; ctx.fillRect(13, gy + 2, W - 26, 2)

  if (scene.toolbox && !deps.length) {
    roundRect(ctx, 18, gy - 11, 26, 11, 2); ctx.fillStyle = '#5b6470'; ctx.fill(); ctx.stroke()
    ctx.beginPath(); ctx.moveTo(25, gy - 11); ctx.lineTo(25, gy - 15); ctx.lineTo(37, gy - 15); ctx.lineTo(37, gy - 11); ctx.lineWidth = 1.6; ctx.stroke()
  }
  if (scene.smoke) drawSmoke(ctx, cx + c * 0.3, gy - c * 1.25, 0.8, t, still)
  const marks = [-0.8, 0.78, -1.08]
  for (let k = 0; k < Math.min(scene.scorches, marks.length); k++) drawScorch(ctx, cx + marks[k]! * c, gy + 4, 5, k + 2)
  deps.forEach((number, k) => {
    const gx = 26 + k * 30
    drawGhost(ctx, gx, gy, 20, '#' + number)
    drawTether(ctx, cx - c * 0.5, gy - c * 0.55, gx + 8, gy - 14, 10)
  })

  // Shadow, then the character.
  ctx.beginPath(); ctx.ellipse(cx, gy + 1, c * 0.55, 3, 0, 0, TAU); ctx.fillStyle = 'rgba(0,0,0,.35)'; ctx.fill()
  const m = cubeMotion(t, hop, seed, c, scene.mood, still)
  drawCube(ctx, cx + m.dx, gy + m.dy, c, STAGE_FILL[scene.stage], {
    ...moodFace(scene.mood, c), accessory: scene.accessory, sx: m.sx, sy: m.sy, blink: m.blink,
  })
  if (scene.beacon) {
    const bx = W - 26
    roundRect(ctx, bx - 1.5, gy - 14, 3, 14, 1); ctx.fillStyle = '#4a4f57'; ctx.fill()
    drawBeacon(ctx, bx, gy - 15, 1, t, still)
  }
  if (scene.zzz) drawZzz(ctx, cx + c * 0.5, gy - c - 4, 1.2, t, still)
  if (scene.confetti) drawConfetti(ctx, W, gy, t, still)
}

// ---------------------------------------------------------------------------
// The card portrait: the issue's cube alone in a small square, with only the
// light props a list can carry. One still frame; `blink` closes open eyes.
// ---------------------------------------------------------------------------
export interface PortraitLook {
  stage: StageId
  mood: CubeMood
  accessory: CubeAccessory | null
  // Waits for the operator: an amber glow and a beacon dot.
  beacon: boolean
  zzz: boolean
  // Failed attempts, drawn as up to three scorch dots on the floor.
  scorches: number
  // Merged: a confetti tint behind the cube.
  confetti: boolean
}

export function portraitFromScene(scene: BenchScene): PortraitLook {
  const { stage, mood, accessory, beacon, zzz, scorches, confetti } = scene
  return { stage, mood, accessory, beacon, zzz, scorches, confetti }
}

// A stable name for a portrait, so a card redraws only when its look changes.
export function portraitKey(look: PortraitLook): string {
  return [look.stage, look.mood, look.accessory ?? 'plain', look.beacon ? 'beacon' : '', look.zzz ? 'zzz' : '',
    look.scorches > 0 ? `scorch-${Math.min(look.scorches, 3)}` : '', look.confetti ? 'confetti' : ''].filter(Boolean).join(' ')
}

export function drawPortrait(ctx: CanvasRenderingContext2D, S: number, look: PortraitLook, blink = false) {
  ctx.clearRect(0, 0, S, S)
  ctx.save()
  roundRect(ctx, 0.5, 0.5, S - 1, S - 1, S * 0.22)
  ctx.fillStyle = look.confetti ? '#18241d' : '#16191f'; ctx.fill(); ctx.clip()
  const floor = Math.round(S * 0.84), c = Math.round(S * 0.44)
  const cx = S / 2 - (look.accessory === 'magnifier' ? S * 0.07 : 0)
  if (look.beacon) drawAmberGlow(ctx, cx, floor - c * 0.5, c * 1.3, 0, true)
  if (look.confetti) {
    const cols = [STAGE_FILL.merged, STAGE_FILL.approval, STAGE_FILL.build, STAGE_FILL.audit, '#f08bb0']
    for (let i = 0; i < 7; i++) {
      ctx.save(); ctx.translate((i * 17.3 + 6) % (S - 8) + 4, (i * 11.7 + 5) % (S * 0.4) + 4); ctx.rotate(i * 1.3)
      ctx.globalAlpha = 0.75; ctx.fillStyle = cols[i % cols.length]!; ctx.fillRect(-2, -1, 4, 2); ctx.restore()
    }
  }
  ctx.fillStyle = '#23272e'; ctx.fillRect(0, floor, S, S - floor)
  const marks = [-0.8, 0.82, -1.08]
  for (let k = 0; k < Math.min(look.scorches, marks.length); k++) drawScorch(ctx, cx + marks[k]! * c, floor + 2, 2.6, k + 2)
  ctx.beginPath(); ctx.ellipse(cx, floor + 0.5, c * 0.55, 2, 0, 0, TAU); ctx.fillStyle = 'rgba(0,0,0,.35)'; ctx.fill()
  drawCube(ctx, cx, floor, c, STAGE_FILL[look.stage], { ...moodFace(look.mood, c), accessory: look.accessory, blink })
  if (look.zzz) drawZzz(ctx, cx + c * 0.4, floor - c - 1, 0.7, 0, true)
  ctx.restore()
  if (look.beacon) {
    const r = Math.max(3, S * 0.065)
    ctx.beginPath(); ctx.arc(S - r - 4, r + 4, r, 0, TAU); ctx.fillStyle = '#ffd27a'; ctx.fill()
    ctx.lineWidth = 1.2; ctx.strokeStyle = CUBE_INK; ctx.stroke()
  }
}

// ---------------------------------------------------------------------------
// The discussion agent's avatar: a little worker on two feet in its persona's
// colour with a small mark above it, whose face follows the conversation. Looking around
// while Forge finds someone, focused while it works, happy with an answer,
// eyes shut once it has left. `t` animates; `still` is the reduced-motion frame.
// ---------------------------------------------------------------------------
export type AgentMood = 'looking' | 'busy' | 'happy' | 'left'
export type AgentMark = 'spark' | 'bolt' | 'star' | null

export function agentFace(mood: AgentMood, c: number, t: number, still: boolean): Pick<CubeOptions, 'eyes' | 'look' | 'lookY' | 'mouth'> & { dx: number; dy: number } {
  switch (mood) {
    // Peeking left and right, as if looking for someone.
    case 'looking': return { eyes: 'open', look: still ? c * 0.09 : Math.sin(t / 650) * c * 0.11, lookY: -c * 0.02, mouth: 'o', dx: 0, dy: still ? 0 : -Math.max(0, Math.sin(t / 325)) * c * 0.04 }
    case 'busy': return { ...moodFace('busy', c), dx: still ? 0 : Math.sin(t / 70) * c * 0.012, dy: 0 }
    case 'happy': return { ...moodFace('happy', c), dx: 0, dy: 0 }
    default: return { ...moodFace('rest', c), mouth: 'flat', dx: 0, dy: 0 }
  }
}

export function drawMark(ctx: CanvasRenderingContext2D, mark: AgentMark, x: number, y: number, s: number, fill: string) {
  if (!mark) return
  ctx.save(); ctx.translate(x, y)
  ctx.lineWidth = Math.max(1, s * 0.14); ctx.strokeStyle = CUBE_INK; ctx.lineJoin = 'round'
  ctx.beginPath()
  if (mark === 'bolt') {
    // A little lightning-bolt antenna.
    ctx.moveTo(s * 0.15, -s); ctx.lineTo(-s * 0.35, -s * 0.05); ctx.lineTo(s * 0.05, -s * 0.05)
    ctx.lineTo(-s * 0.2, s * 0.75); ctx.lineTo(s * 0.45, -s * 0.3); ctx.lineTo(s * 0.02, -s * 0.3); ctx.closePath()
  } else {
    const points = mark === 'star' ? 5 : 4, outer = s * 0.8, inner = mark === 'star' ? s * 0.36 : s * 0.22
    for (let i = 0; i < points * 2; i++) {
      const r = i % 2 ? inner : outer, a = -Math.PI / 2 + (i * Math.PI) / points
      if (i) ctx.lineTo(Math.cos(a) * r, Math.sin(a) * r)
      else ctx.moveTo(Math.cos(a) * r, Math.sin(a) * r)
    }
    ctx.closePath()
  }
  ctx.fillStyle = mark === 'spark' ? '#ffe2b8' : mark === 'star' ? '#e6e3ff' : fill
  ctx.fill(); ctx.stroke(); ctx.restore()
}

// The agent is a worker, not an issue: a machine-like body on two little legs
// with feet, like the machines on the factory floor, its face on the body and
// its persona's mark above. Issues stay rounded-square cube heads. Everything
// is laid out in an S×S square; `workerShape` is the geometry, for tests.
export interface WorkerShape {
  body: { x: number; y: number; w: number; h: number; r: number }
  legs: { x: number; y: number; w: number; h: number }[]
  feet: { x: number; y: number; w: number; h: number }[]
  face: { x: number; y: number; c: number }
  mark: { x: number; y: number; s: number }
}

export function workerShape(S: number): WorkerShape {
  const floor = S * 0.97, cx = S / 2
  const fw = S * 0.2, fh = S * 0.075, lw = S * 0.095, lh = S * 0.13
  const bw = S * 0.72, bh = S * 0.5
  const bodyBottom = floor - fh - lh + S * 0.02
  const top = bodyBottom - bh
  const spread = bw * 0.27
  return {
    body: { x: cx - bw / 2, y: top, w: bw, h: bh, r: S * 0.11 },
    legs: [-1, 1].map(side => ({ x: cx + side * spread - lw / 2, y: bodyBottom - S * 0.03, w: lw, h: floor - fh - bodyBottom + S * 0.04 })),
    feet: [-1, 1].map(side => ({ x: cx + side * spread - fw / 2 + side * S * 0.03, y: floor - fh, w: fw, h: fh })),
    face: { x: cx, y: bodyBottom - bh * 0.06, c: bw * 0.8 },
    mark: { x: cx + bw * 0.32, y: top - S * 0.09, s: S * 0.15 },
  }
}

// Busy: little alternating steps. Every other mood stands; still never moves.
export function workerSteps(mood: AgentMood, S: number, t: number, still: boolean): [number, number] {
  if (still || mood !== 'busy') return [0, 0]
  const k = Math.sin(t / 110)
  return [Math.max(0, k) * S * 0.035, Math.max(0, -k) * S * 0.035]
}

export function drawAgent(ctx: CanvasRenderingContext2D, S: number, fill: string, mark: AgentMark, mood: AgentMood, t = 0, still = true) {
  ctx.clearRect(0, 0, S, S)
  const face = agentFace(mood, workerShape(S).face.c, t, still)
  drawWorker(ctx, S, fill, mark, face, workerSteps(mood, S, t, still), mood === 'left' ? 0.7 : 1)
}

export type WorkerFace = Pick<CubeOptions, 'eyes' | 'look' | 'lookY' | 'mouth' | 'blink'> & { dx: number; dy: number }

// A worker in its S×S square at the origin; callers translate to place it
// elsewhere (the sign-in scene's shop floor). `lift` raises each foot.
export function drawWorker(ctx: CanvasRenderingContext2D, S: number, fill: string, mark: AgentMark, face: WorkerFace, lift: [number, number] = [0, 0], alpha = 1) {
  const shape = workerShape(S)
  // A hop lifts the whole worker; a step lifts one foot and the body a little.
  const bob = face.dy - Math.max(lift[0], lift[1]) * 0.5
  const lw = Math.max(1.2, S * 0.045)
  ctx.save()
  ctx.globalAlpha = alpha
  ctx.lineWidth = lw; ctx.strokeStyle = CUBE_INK; ctx.lineJoin = 'round'
  // Legs and feet first, so the body sits on top of them.
  shape.legs.forEach((leg, i) => {
    const up = lift[i]! - face.dy
    roundRect(ctx, leg.x + face.dx, leg.y + bob, leg.w, leg.h - up - bob, leg.w * 0.4); ctx.fillStyle = fill; ctx.fill(); ctx.stroke()
    const foot = shape.feet[i]!
    roundRect(ctx, foot.x + face.dx, foot.y - up, foot.w, foot.h, foot.h * 0.5); ctx.fillStyle = '#2a2f37'; ctx.fill(); ctx.stroke()
  })
  const b = shape.body
  roundRect(ctx, b.x + face.dx, b.y + bob, b.w, b.h, b.r); ctx.fillStyle = fill; ctx.fill(); ctx.stroke()
  // The machines' lighter top band.
  const inset = lw + 0.6
  roundRect(ctx, b.x + face.dx + inset, b.y + bob + inset, b.w - 2 * inset, b.h * 0.2, b.r * 0.6); ctx.fillStyle = 'rgba(255,255,255,.22)'; ctx.fill()
  ctx.save(); ctx.translate(shape.face.x + face.dx, shape.face.y + bob)
  drawFace(ctx, shape.face.c, { eyes: face.eyes, look: face.look, lookY: face.lookY, mouth: face.mouth, blink: face.blink }, 12)
  ctx.restore()
  drawMark(ctx, mark, shape.mark.x + face.dx, shape.mark.y + bob, shape.mark.s, fill)
  ctx.restore()
}
