// The sign-in page: which friendly notice to show for what the auth flow
// sent, and the small decorative factory scene above the card. The scene is
// pure canvas drawing from a clock: no data, no requests, nothing about the
// instance. Same cubes, palette and personas as the factory line.
import { CUBE_INK, cubeMotion, drawCube, drawMark, drawSmoke, drawZzz, moodFace, roundRect, type CubeAccessory, type CubeMood } from './cube'
import { LINE_LOOKS, lineMix } from './factoryLine'
import { PERSONAS } from './personas'

// The host cube's face: waving hello, worried after a failed attempt, asleep
// while the sign-in service is down, hopeful when it brings you back somewhere.
export type SignInMood = 'waving' | 'worried' | 'sleepy' | 'hopeful'
export type SignInTone = 'coral' | 'amber' | 'mint'

export interface SignInNotice {
  kind: 'unavailable' | 'failed' | 'return'
  mood: SignInMood
  tone: SignInTone
  // Errors are announced; the return hint is a polite status.
  role: 'alert' | 'status'
  title: string
  text: string
}

export interface SignInInput {
  // `/auth/github/callback` sends `?error=sign_in` for every failed attempt:
  // not authorized, expired, wrong browser, or GitHub refused. It names no
  // account and gives no reason, so neither does the page.
  error?: unknown
  // The operator middleware adds `return_to` only for /settings/agents.
  returnTo?: unknown
  // `/api/auth/session` did not answer.
  unavailable: boolean
}

export function signInNotice({ error, returnTo, unavailable }: SignInInput): SignInNotice | null {
  if (unavailable) {
    return {
      kind: 'unavailable', mood: 'sleepy', tone: 'amber', role: 'alert',
      title: 'The sign-in desk is napping',
      text: 'Forge couldn’t reach its sign-in service. Give it a moment, then try again.',
    }
  }
  if (error) {
    return {
      kind: 'failed', mood: 'worried', tone: 'coral', role: 'alert',
      title: 'That sign-in didn’t go through',
      text: 'This GitHub account may not be on the list for this Forge, or the sign-in took too long and expired. Try again, or ask your Forge administrator to add your account.',
    }
  }
  if (typeof returnTo === 'string' && returnTo.startsWith('/settings/agents')) {
    return {
      kind: 'return', mood: 'hopeful', tone: 'mint', role: 'status',
      title: 'Almost there',
      text: 'Sign in to finish connecting your agent. You’ll come straight back here.',
    }
  }
  return null
}

// ---------------------------------------------------------------------------
// The scene
// ---------------------------------------------------------------------------
const TAU = Math.PI * 2
// The reduced-motion frame: a moment where the line looks busy and tidy.
export const SCENE_STILL_T = 2600

const clamp = (v: number, a: number, b: number) => Math.max(a, Math.min(b, v))

export interface SceneLayout {
  s: number; pad: number; g0: number; ch: number; c: number
  tx: number; bedW: number; cabW: number
  mw: number; leg: number; MH: number; blockH: number
  press: number; eye: number; scanner: number; hop: number
  x0: number; xe: number; n: number; speed: number
  floor: number; pc: number
  cast: { name: 'Ember' | 'Bolt' | 'Nova'; x: number; size: number }[]
}

export function sceneLayout(W: number, H: number): SceneLayout {
  const s = clamp(Math.min(W / 520, H / 220), 0.55, 1.15)
  const pad = 14 * s, g0 = Math.round(H * 0.58), ch = Math.max(7, 9 * s), c = Math.round(20 * s)
  const bedW = 46 * s, cabW = 26 * s, tx = W - pad - bedW - cabW - 4
  const span = tx - pad
  const mw = c + 26 * s, leg = 7 * s, MH = Math.round(66 * s), blockH = MH - c - 12 * s
  const x0 = pad - c * 0.5, xe = tx + bedW * 0.55
  const n = Math.max(3, Math.round((xe - x0) / (86 * s)))
  const floor = H - 16 * s, pc = Math.round(32 * s)
  return {
    s, pad, g0, ch, c, tx, bedW, cabW, mw, leg, MH, blockH,
    press: pad + span * 0.24, eye: pad + span * 0.5, scanner: pad + span * 0.76, hop: tx - 16 * s,
    x0, xe, n, speed: 40 * s, floor, pc,
    cast: [
      { name: 'Ember', x: Math.max(pad + 24 * s, W * 0.12), size: Math.round(40 * s) },
      { name: 'Bolt', x: W * 0.42, size: pc },
      { name: 'Nova', x: W * 0.58, size: pc },
    ],
  }
}

// Where each line cube is at time t (ms): evenly spaced, so the loop is seamless.
export function lineCubes(L: SceneLayout, t: number): number[] {
  const len = L.xe - L.x0, sp = len / L.n, d = (t / 1000) * L.speed
  return Array.from({ length: L.n }, (_, i) => L.x0 + ((d + i * sp) % len))
}

// A cube takes on each stage's colour as it passes through that machine.
function cubeFill(L: SceneLayout, x: number): string {
  const steps: [number, string][] = [
    [L.press, LINE_LOOKS.build.fill], [L.eye, LINE_LOOKS.review.fill],
    [L.scanner, LINE_LOOKS.audit.fill], [L.hop, LINE_LOOKS.merged.fill],
  ]
  let col = LINE_LOOKS.queued.fill
  const w = L.mw * 0.6
  for (const [p, next] of steps) col = lineMix(col, next, (x - (p - w / 2)) / w)
  return col
}

function cubeAccessory(L: SceneLayout, x: number): CubeAccessory | null {
  if (L.c < 16) return null
  if (x > L.hop) return 'bow'
  if (x > L.scanner + L.mw * 0.3) return 'badge'
  if (x > L.eye + L.mw * 0.3) return 'magnifier'
  if (x > L.press + L.mw * 0.3) return 'hat'
  return null
}

const nearest = (xs: number[], p: number) => xs.reduce((a, x) => Math.abs(x - p) < Math.abs(a - p) ? x : a, Infinity)

function drawMachineFrame(ctx: CanvasRenderingContext2D, L: SceneLayout, cx: number, body: string, hi: string) {
  const top = L.g0 - L.MH, mx = cx - L.mw / 2
  ctx.lineWidth = 1.6; ctx.strokeStyle = CUBE_INK; ctx.fillStyle = body
  roundRect(ctx, mx, top + 6, L.leg, L.MH - 6, 3); ctx.fill(); ctx.stroke()
  roundRect(ctx, mx + L.mw - L.leg, top + 6, L.leg, L.MH - 6, 3); ctx.fill(); ctx.stroke()
  roundRect(ctx, mx - 3 * L.s, top, L.mw + 6 * L.s, L.blockH, 7 * L.s); ctx.fillStyle = body; ctx.fill(); ctx.stroke()
  roundRect(ctx, mx, top + 3, L.mw, 5 * L.s, 3); ctx.fillStyle = hi; ctx.fill()
}

function lamp(ctx: CanvasRenderingContext2D, x: number, y: number, r: number, on: boolean) {
  ctx.beginPath(); ctx.arc(x, y, r, 0, TAU)
  ctx.fillStyle = on ? LINE_LOOKS.merged.fill : '#2a2f36'
  if (on) { ctx.shadowColor = LINE_LOOKS.merged.fill; ctx.shadowBlur = 6 }
  ctx.fill(); ctx.shadowBlur = 0
}

// The host's little wave: a mitten on a stub at the cube's upper right.
function drawWave(ctx: CanvasRenderingContext2D, x: number, y: number, c: number, fill: string, t: number, still: boolean) {
  const a = -1.05 + (still ? 0.25 : Math.sin(t / 170) * 0.42)
  const px = x + c * 0.47, py = y - c * 0.5, len = c * 0.42
  const hx = px + Math.cos(a) * len, hy = py + Math.sin(a) * len
  ctx.lineCap = 'round'; ctx.strokeStyle = CUBE_INK; ctx.lineWidth = Math.max(2.4, c * 0.13)
  ctx.beginPath(); ctx.moveTo(px, py); ctx.lineTo(hx, hy); ctx.stroke()
  ctx.strokeStyle = fill; ctx.lineWidth = Math.max(1, c * 0.06); ctx.stroke()
  ctx.beginPath(); ctx.arc(hx, hy, c * 0.14, 0, TAU); ctx.fillStyle = fill; ctx.fill()
  ctx.lineWidth = Math.max(1.2, c * 0.04); ctx.strokeStyle = CUBE_INK; ctx.stroke()
}

function drawSweat(ctx: CanvasRenderingContext2D, x: number, y: number, s: number, t: number, still: boolean) {
  const ph = still ? 0.3 : (t / 1400) % 1
  ctx.globalAlpha = 1 - ph * 0.8; ctx.fillStyle = '#9ccfe8'
  const dy = y + ph * 8 * s
  ctx.beginPath(); ctx.moveTo(x, dy - 4 * s); ctx.quadraticCurveTo(x + 3 * s, dy, x, dy + 2 * s); ctx.quadraticCurveTo(x - 3 * s, dy, x, dy - 4 * s); ctx.fill()
  ctx.globalAlpha = 1
}

const CUBE_MOOD: Record<SignInMood, CubeMood> = { waving: 'happy', hopeful: 'hopeful', worried: 'worried', sleepy: 'sleep' }

// One frame of the scene, W x H in CSS pixels, at time t (ms). `still` draws
// the reduced-motion frame. While the sign-in service sleeps, so does the line.
export function drawSignInScene(ctx: CanvasRenderingContext2D, W: number, H: number, t: number, still: boolean, mood: SignInMood) {
  const L = sceneLayout(W, H)
  const { s, g0, c, tx } = L
  const asleep = mood === 'sleepy'
  const lineT = still || asleep ? SCENE_STILL_T : t
  const xs = lineCubes(L, lineT)

  ctx.clearRect(0, 0, W, H)
  // backdrop: wall, a few warm work lights, floor
  ctx.fillStyle = '#16191f'; ctx.fillRect(0, 0, W, g0 + 14 * s)
  ctx.fillStyle = '#121418'; ctx.fillRect(0, g0 + 14 * s, W, H)
  ctx.fillStyle = '#1c2026'; ctx.fillRect(0, g0 + 14 * s, W, 2)
  for (const fx of [0.18, 0.5, 0.82]) {
    const lx = W * fx, g = ctx.createRadialGradient(lx, 0, 2, lx, 0, 90 * s)
    g.addColorStop(0, asleep ? 'rgba(160,168,178,.06)' : 'rgba(127,214,160,.08)'); g.addColorStop(1, 'rgba(127,214,160,0)')
    ctx.fillStyle = g; ctx.fillRect(lx - 90 * s, 0, 180 * s, 90 * s)
    ctx.fillStyle = '#2a2f37'; ctx.fillRect(lx - 9 * s, 0, 18 * s, 3 * s)
  }

  // conveyor
  const cx0 = L.pad, cx1 = tx - 6, ch = L.ch
  roundRect(ctx, cx0, g0, cx1 - cx0, ch, ch / 2); ctx.fillStyle = '#2a2f37'; ctx.fill(); ctx.lineWidth = 1.6; ctx.strokeStyle = CUBE_INK; ctx.stroke()
  const roll = (lineT / 1000) * L.speed / (ch * 0.32), wr = ch * 0.32
  for (let x = cx0 + ch; x < cx1 - ch / 2; x += 15 * s) {
    ctx.beginPath(); ctx.arc(x, g0 + ch / 2, wr, 0, TAU); ctx.fillStyle = '#434a55'; ctx.fill()
    ctx.strokeStyle = '#20242a'; ctx.lineWidth = 1
    ctx.beginPath(); ctx.moveTo(x - Math.cos(roll) * wr, g0 + ch / 2 - Math.sin(roll) * wr); ctx.lineTo(x + Math.cos(roll) * wr, g0 + ch / 2 + Math.sin(roll) * wr); ctx.stroke()
  }

  // line cubes, behind the machine fronts
  const tyb = g0 + 3 * s
  const pressNear = nearest(xs, L.press), pressD = Math.abs(pressNear - L.press)
  const stamp = asleep ? 0 : clamp(1 - pressD / (c * 0.7), 0, 1)
  for (const x of xs) {
    let y = g0, sx = 1, sy = 1, alpha = clamp((x - L.x0) / (c * 0.8), 0, 1)
    if (x > L.hop) {
      const k = clamp((x - L.hop) / (L.xe - L.hop), 0, 1)
      y = g0 + (tyb - g0) * k - Math.sin(Math.PI * Math.min(1, k / 0.8)) * 24 * s
      if (k > 0.8) y = tyb + ((k - 0.8) / 0.2) * c * 0.55
    } else if (Math.abs(x - L.press) < c * 0.45 && !asleep) {
      sy = 1 - 0.14 * (1 - Math.abs(x - L.press) / (c * 0.45)); sx = 1 + (1 - sy) * 0.8
    }
    drawCube(ctx, x, y, c, cubeFill(L, x), { eyes: x > L.hop ? 'happy' : 'open', look: c * 0.07, sx, sy, alpha, accessory: cubeAccessory(L, x), mouth: null })
  }

  // the press: pistons stamp each cube that passes, smoke from the chimney
  {
    const top = g0 - L.MH, mx = L.press - L.mw / 2
    const chx = mx + L.mw - L.leg - 10 * s
    roundRect(ctx, chx, top - 10 * s, 8 * s, 14 * s, 2); ctx.fillStyle = '#28302f'; ctx.fill(); ctx.lineWidth = 1.6; ctx.strokeStyle = CUBE_INK; ctx.stroke()
    if (!asleep) drawSmoke(ctx, chx + 4 * s, top - 12 * s, s, t, still)
    const ext = stamp * 7 * s
    ctx.fillStyle = '#9aa3ad'; ctx.fillRect(L.press - 2 * s, top + L.blockH - 2, 4 * s, 4 * s + ext)
    roundRect(ctx, L.press - c * 0.34, top + L.blockH + 2 * s + ext, c * 0.68, 4 * s, 1.5); ctx.fillStyle = '#c4ccd4'; ctx.fill(); ctx.lineWidth = 1.2; ctx.strokeStyle = CUBE_INK; ctx.stroke()
    drawMachineFrame(ctx, L, L.press, '#2f6f69', '#3f8a83')
    lamp(ctx, mx + 6 * s, top + 12 * s, Math.max(1.8, 2.4 * s), stamp > 0.2)
  }

  // the reviewer's eye: it follows the cube passing under it
  {
    drawMachineFrame(ctx, L, L.eye, '#3b5a80', '#4d72a0')
    const top = g0 - L.MH, ex = L.eye, ey = top + L.blockH * 0.52, er = Math.min(L.blockH * 0.32, 10 * s)
    ctx.beginPath(); ctx.arc(ex, ey, er, 0, TAU); ctx.fillStyle = '#e9eef3'; ctx.fill(); ctx.lineWidth = 1.6; ctx.strokeStyle = CUBE_INK; ctx.stroke()
    const near = nearest(xs, ex), look = clamp((near - ex) / L.mw, -1, 1) * er * 0.45
    ctx.beginPath(); ctx.arc(ex + look, ey + er * 0.25, er * 0.42, 0, TAU); ctx.fillStyle = '#12151a'; ctx.fill()
    if (asleep) {
      // eyelid shut: the machine is asleep too
      ctx.beginPath(); ctx.arc(ex, ey, er + 1, 0, TAU); ctx.fillStyle = '#3b5a80'; ctx.fill(); ctx.stroke()
      ctx.beginPath(); ctx.arc(ex, ey - er * 0.2, er * 0.75, Math.PI * 0.15, Math.PI * 0.85); ctx.stroke()
    }
  }

  // the scanner: a violet beam while a cube is under it
  {
    const top = g0 - L.MH, scanning = !asleep && Math.abs(nearest(xs, L.scanner) - L.scanner) < L.mw * 0.4
    if (scanning) {
      const by = top + L.blockH, sweep = still ? 0 : Math.sin(t / 160) * (L.mw * 0.18)
      const g = ctx.createLinearGradient(0, by, 0, g0)
      g.addColorStop(0, 'rgba(165,139,239,.5)'); g.addColorStop(1, 'rgba(165,139,239,.06)')
      ctx.fillStyle = g; ctx.beginPath(); ctx.moveTo(L.scanner - 5 * s, by); ctx.lineTo(L.scanner + 5 * s, by)
      ctx.lineTo(L.scanner + sweep + c * 0.6, g0); ctx.lineTo(L.scanner + sweep - c * 0.6, g0); ctx.closePath(); ctx.fill()
    }
    ctx.strokeStyle = CUBE_INK; ctx.lineWidth = 1.6; ctx.beginPath(); ctx.moveTo(L.scanner, top); ctx.lineTo(L.scanner, top - 10 * s); ctx.stroke()
    drawMachineFrame(ctx, L, L.scanner, '#6a5a48', '#85725d')
    ctx.beginPath(); ctx.arc(L.scanner, top - 11 * s, 2.6 * s, 0, TAU)
    ctx.fillStyle = scanning ? LINE_LOOKS.merged.fill : '#3b3f45'; ctx.fill(); ctx.strokeStyle = CUBE_INK; ctx.lineWidth = 1.6; ctx.stroke()
    const slit = top + L.blockH * 0.5
    roundRect(ctx, L.scanner - L.mw * 0.3, slit - 2 * s, L.mw * 0.6, 4 * s, 2); ctx.fillStyle = scanning ? '#c3b1f5' : '#3a3229'; ctx.fill()
  }

  // the truck: two finished cubes already aboard; the bed bumps as one lands
  {
    const landing = xs.some(x => x > L.hop + (L.xe - L.hop) * 0.75)
    const T = tx, ty = tyb - (landing && !still ? 1.5 : 0)
    ctx.fillStyle = '#1b1f25'; ctx.fillRect(tx - 6, g0 + ch + 3 * s, W - tx + 6, 3)
    roundRect(ctx, T, ty - c * 0.9, 4 * s, c * 0.9 + 4, 2); ctx.fillStyle = '#3d8f63'; ctx.fill(); ctx.lineWidth = 1.6; ctx.strokeStyle = CUBE_INK; ctx.stroke()
    roundRect(ctx, T, ty - c * 0.45, L.bedW, c * 0.45 + 5 * s, 3); ctx.fillStyle = '#4fa877'; ctx.fill(); ctx.stroke()
    roundRect(ctx, T + L.bedW + 2, ty - c - 6 * s, L.cabW, c + 11 * s, 5 * s); ctx.fillStyle = '#5cbf89'; ctx.fill(); ctx.stroke()
    roundRect(ctx, T + L.bedW + 2 + L.cabW * 0.35, ty - c - 2 * s, L.cabW * 0.5, c * 0.45, 2); ctx.fillStyle = '#1c2a33'; ctx.fill(); ctx.lineWidth = 1.2; ctx.stroke()
    for (const wx of [T + 9 * s, T + L.bedW + L.cabW - 6 * s]) {
      ctx.beginPath(); ctx.arc(wx, ty + 6 * s, 5.5 * s, 0, TAU); ctx.fillStyle = '#1a1d22'; ctx.fill(); ctx.lineWidth = 1.6; ctx.strokeStyle = CUBE_INK; ctx.stroke()
      ctx.beginPath(); ctx.arc(wx, ty + 6 * s, 2 * s, 0, TAU); ctx.fillStyle = '#8a929c'; ctx.fill()
    }
  }

  // the cast, on the shop floor: Ember greets you, Bolt and Nova watch the line
  const hostMood = CUBE_MOOD[mood]
  for (const [i, who] of L.cast.entries()) {
    const p = PERSONAS[who.name === 'Ember' ? 'claude' : who.name === 'Bolt' ? 'codex' : 'grok']!
    const host = who.name === 'Ember', size = who.size
    const m = cubeMotion(t, -1e9, i * 1.7 + 0.4, size, host ? hostMood : asleep ? 'sleep' : 'hopeful', still)
    const x = who.x + m.dx, y = L.floor + m.dy
    ctx.beginPath(); ctx.ellipse(who.x, L.floor + 1, size * 0.5, size * 0.12, 0, 0, TAU); ctx.fillStyle = 'rgba(0,0,0,.35)'; ctx.fill()
    let face = moodFace(host ? hostMood : asleep ? 'sleep' : 'hopeful', size)
    if (!host && !asleep) {
      // Bolt and Nova track the nearest cube on the line; after a failed
      // attempt they glance over at Ember instead.
      const target = mood === 'worried' ? L.cast[0]!.x : nearest(xs, who.x)
      face = { ...face, eyes: 'open', look: clamp((target - who.x) / 80, -1, 1) * size * 0.1, lookY: mood === 'worried' ? 0 : -size * 0.07, mouth: mood === 'worried' ? 'flat' : 'smile' }
    }
    drawCube(ctx, x, y, size, p.fill, { ...face, sx: m.sx, sy: m.sy, blink: m.blink })
    drawMark(ctx, p.mark, x + size * 0.42, y - size - size * 0.12, size * 0.26, p.fill)
    if (host && (mood === 'waving' || mood === 'hopeful')) drawWave(ctx, x, y, size, p.fill, t, still)
    if (host && mood === 'worried') drawSweat(ctx, x - size * 0.58, y - size * 0.8, s, t, still)
    if (host && asleep) drawZzz(ctx, x + size * 0.45, y - size - 2 * s, s, t, still)
  }
}
