<script setup lang="ts">
import type { CardModel, ColumnRole, FactoryView, IssueGroups } from '~/utils/factory'
import {
  LINE_REASONS, LINE_REASON_SHORT, LINE_REASON_TEXT, LINE_SHORT_LABELS,
  lineAnalyse, lineBlocker, lineDiff, lineFill, lineGroups, lineMix, lineHeadline, lineHeadlineText, lineStation, lineState, lineSummary, lineTooltip,
  type AttentionReason, type HeadlinePart, type LineInfo, type LineItem, type LineStage, type LineState,
} from '~/utils/factoryLine'
import { panelQuery, panelTarget } from '~/utils/panel'
import { accountsHeadline } from '~/utils/accounts'
import { lineBayCard, lineIssueCard, lineListCard, placeCard, type LineCard, type Rect } from '~/utils/lineCard'
import { needsYouRows, type NeedsFilter } from '~/utils/needsYou'
import { unblockCounts } from '~/utils/unblocks'
import { CUBE_INK as INK, drawCube, roundRect as rr, type CubeOptions } from '~/utils/cube'
import { BAY_GLYPHS, GLYPH_INK, glyphMotion, type GlyphPart } from '~/utils/bayGlyphs'

// The factory line: a 200px strip that draws the board as a toy factory.
// Every cube is an issue from the last poll; a cube moves only when a poll
// puts its issue somewhere else, and the machines only animate while a
// worker is actually running. The drawing is the "Toy factory" prototype,
// ported onto live state from utils/factoryLine.ts.
const props = defineProps<{ view: FactoryView | null; now: number; groups?: IssueGroups }>()
// `attention` opens the list of issues that need the operator, filtered to
// one bay when a bay was clicked.
const emit = defineEmits<{ open: [card: CardModel]; attention: [filter: NeedsFilter] }>()

const route = useRoute()
const router = useRouter()

const state = computed(() => lineState(props.view, props.now, props.groups))
const info = computed(() => state.value ? lineAnalyse(state.value) : null)
const { accounts } = useCodingAccounts()
const parts = computed<HeadlinePart[]>(() => info.value ? lineHeadline(info.value, accountsHeadline(accounts.value)) : [])
const label = computed(() => lineHeadlineText(parts.value))
const summary = computed(() => state.value && info.value ? lineSummary(state.value, info.value) : '')
const links = computed(() => (state.value?.items ?? []).map(item => ({
  item,
  text: lineTooltip(item, (state.value?.repos ?? 0) > 1),
  href: router.resolve({ path: route.path, query: panelQuery(route.query, panelTarget(item.card)) }).href,
})))

const canvas = ref<HTMLCanvasElement | null>(null)
// Where each cube rests, for the hidden links (and the tests that click them).
const spots = ref<Record<string, { x: number; y: number }>>({})
// The hover card: what it says, the strip area it describes (in viewport
// pixels), and where it sits once measured. It is teleported to the body
// and fixed, so the strip's rounded overflow never clips it.
const tip = ref<{ key: string; card: LineCard; anchor: Rect } | null>(null)
const tipEl = ref<HTMLElement | null>(null)
const tipAt = ref<{ left: number; top: number } | null>(null)
// The crate and its bays, for the keyboard buttons laid over them.
const regions = ref<{ crate: Rect; bays: (Rect & { reason: AttentionReason })[] }>({ crate: { x: 0, y: 0, w: 0, h: 0 }, bays: [] })
const unblocks = computed(() => unblockCounts(props.view?.issues ?? []))
const rawIssues = computed(() => new Map((props.view?.issues ?? []).map(issue => [issue.key, issue])))
const crateRows = computed(() => needsYouRows(state.value, props.view))
const needCount = computed(() => info.value?.attention ?? 0)

// ---------------------------------------------------------------------------
// Scene model: the live items plus the cubes still on their way out.
// ---------------------------------------------------------------------------
type Item = LineItem & { mergedAt?: number; order: number }
interface Groups extends Record<LineStage, Item[]> {}
interface Vis { x: number; y: number; fx: number; fy: number; tx: number; ty: number; t0: number; dur: number; stage: string; hop: boolean; land: number; k: number; dist: number; loaded?: number; size: number
  // The colour blends from cf to ct over the move, so a cube matures as it hops.
  col: string; cf: string; ct: string; ct0: number; cdur: number }
interface Hit { kind: 'issue' | 'reason' | 'neck' | 'crate'; id?: string; reason?: AttentionReason; stage?: ColumnRole; waiting?: number; x: number; y: number; w: number; h: number }
interface Ghost { x: number; y: number; size: number; fill: string; t0: number }
interface Particle { x: number; y: number; vx: number; vy: number; r: number; g: number; life: number; max: number; col: string }

const model = {
  items: [] as Item[], byId: new Map<string, Item>(), groups: lineGroups([]) as Groups,
  info: null as LineInfo | null, slots: { build: 0, review: 0, audit: 0 } as Record<ColumnRole, number>,
  merged: 0, lastMerge: -1e9, fresh: new Set<string>(), seq: 0,
}
const vis = new Map<string, Vis>()
const ghosts: Ghost[] = []
const scene = { W: 0, H: 200, dpr: 1, hits: [] as Hit[], hover: null as Hit | null, snap: true, lastAt: 0, ctx: null as CanvasRenderingContext2D | null }
const toy = { parts: [] as Particle[], plus: [] as { t: number }[], truckLeave: -1e9, smokeAcc: 0 }
let reduced = false
let previous: LineState | null = null
let layoutKey = ''

const COL = { text: '#edf1ee', muted: '#acb7b9', dim: '#565d67', green: '#7fcc9d', amber: '#ecc486', coral: '#ef7d64', panel: '#192024', line: '#303b40' }
let SANS = 'system-ui, -apple-system, sans-serif'
const MONO = 'ui-monospace, "SF Mono", Menlo, monospace'
const FONT = (w: number, px: number, fam?: 'mono') => `${w} ${px}px ${fam === 'mono' ? MONO : SANS}`

const clamp = (v: number, a: number, b: number) => Math.max(a, Math.min(b, v))
const ease = (t: number) => t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2
const rand = (a: number, b: number) => a + Math.random() * (b - a)

function regroup() {
  model.byId = new Map(model.items.map(item => [item.id, item]))
  model.groups = lineGroups(model.items) as Groups
}

function apply(next: LineState | null) {
  if (!next) return
  const diff = lineDiff(previous, next)
  const at = performance.now()
  if (!previous) scene.snap = true
  const was = new Map(model.items.map(item => [item.id, item]))
  const items: Item[] = next.items.map(item => ({ ...item, order: was.get(item.id)?.order ?? model.seq++ }))
  for (const id of diff.entered) model.fresh.add(id)
  // A cube that left the line fades where it stood; one that merged rides
  // the truck first (only with motion: a still frame just counts it).
  for (const id of diff.left) {
    const v = vis.get(id)
    if (v && !reduced) ghosts.push({ x: v.x, y: v.y, size: v.size, fill: v.col, t0: at })
    vis.delete(id)
  }
  for (const id of diff.merged) {
    const old = was.get(id)
    if (old && !reduced) items.push({ ...old, stage: 'merged', mergedAt: at })
    else vis.delete(id)
  }
  // Earlier merges still on their way to the truck stay until they arrive.
  for (const item of model.items) if (item.stage === 'merged' && !items.some(other => other.id === item.id)) items.push(item)
  if (diff.plus && !reduced) {
    model.lastMerge = at
    for (let i = diff.merged.length; i < Math.min(diff.plus, 3); i++) toy.plus.push({ t: at })
  }
  model.items = items
  model.slots = next.slots
  model.merged = next.merged
  model.info = lineAnalyse(next)
  previous = next
  regroup()
  requestDraw()
}

watch(state, apply)

// ---------------------------------------------------------------------------
// Drawing helpers (from the prototype; the cube itself is utils/cube.ts)
// ---------------------------------------------------------------------------
function spacing(ctx: CanvasRenderingContext2D, v: string) { if ('letterSpacing' in ctx) (ctx as unknown as { letterSpacing: string }).letterSpacing = v }
function drawHeadline(ctx: CanvasRenderingContext2D, x: number, y: number, size: number) {
  ctx.font = FONT(500, size); spacing(ctx, '0px'); ctx.textBaseline = 'alphabetic'; ctx.textAlign = 'left'
  const sep = '  ·  '
  const widths = parts.value.map(p => ctx.measureText(p.t).width), sw = ctx.measureText(sep).width
  let cx = x
  parts.value.forEach((p, i) => {
    ctx.fillStyle = COL[p.c]
    ctx.fillText(p.t, cx, y); cx += widths[i]!
    if (i < parts.value.length - 1) { ctx.fillStyle = COL.dim; ctx.fillText(sep, cx, y); cx += sw }
  })
}
function tether(ctx: CanvasRenderingContext2D, x1: number, y1: number, x2: number, y2: number, alpha: number, style: string) {
  ctx.save()
  ctx.globalAlpha = alpha
  ctx.strokeStyle = style; ctx.lineWidth = 1; ctx.setLineDash([2, 3])
  ctx.beginPath(); ctx.moveTo(x1, y1); ctx.bezierCurveTo(x1, y1 - 24, x2, y2 + 30, x2, y2); ctx.stroke()
  ctx.restore()
}
// ---------------------------------------------------------------------------
// Layout: where every station and cube sits for a given width.
// ---------------------------------------------------------------------------
interface Machine { mx: number; mw: number; cap: number; leg: number }
interface Bay { r: AttentionReason; x: number; w: number; label: string }
interface Layout {
  pos: Map<string, { x: number; y: number }>; s: number; c: number; g0: number; gp: number; cols: { x: number; w: number }[]
  machines: Record<ColumnRole, Machine>; gateX: number; tx: number; bedW: number; cabW: number; fy: number; cb: number
  bays: Bay[]; plank: number; crateX: number; crateR: number; needR: number; areaY: number; wx: number; hx: number; pad: number; small: boolean
}
const STAGES: ColumnRole[] = ['build', 'review', 'audit']

function layout(ctx: CanvasRenderingContext2D, W: number): Layout {
  const g = model.groups
  const pad = 16, s = clamp(W / 900, 0.6, 1), c = Math.round(18 * s), g0 = 104, gp = Math.max(3, Math.round(6 * s)), leg = Math.max(5, Math.round(7 * s))
  const wts = [1.2, 1.4, 1.1, 1.1, 1.25, 1.15], tw = wts.reduce((a, b) => a + b, 0)
  const cols: { x: number; w: number }[] = []; let x = pad
  for (const w of wts) { const cw = (W - pad * 2) * w / tw; cols.push({ x, w: cw }); x += cw }
  const pos = new Map<string, { x: number; y: number }>()
  const machines = {} as Record<ColumnRole, Machine>
  STAGES.forEach((st, index) => {
    const i = index + 1
    const cap = Math.max(1, model.slots[st], g[st].length)
    const mw = cap * c + (cap + 1) * gp + leg * 2
    const mx = cols[i]!.x + Math.max(0, (cols[i]!.w - mw) / 2)
    machines[st] = { mx, mw, cap, leg }
    g[st].forEach((is, j) => pos.set(is.id, { x: mx + leg + gp + j * (c + gp) + c / 2, y: g0 }))
  })
  // A line of cubes ending at startRight, wrapping upward; past three rows
  // the cubes squeeze together instead of climbing into the signboard.
  const line = (arr: Item[], startRight: number, maxW: number, tight?: boolean) => {
    let st = c + (tight ? 1 : 4), n = Math.max(1, Math.floor(maxW / st))
    if (arr.length > n * 3) { n = Math.ceil(arr.length / 3); st = Math.max(3, (maxW - c) / Math.max(1, n - 1)) }
    arr.forEach((is, k) => pos.set(is.id, { x: startRight - (k % n) * st - c / 2, y: g0 - Math.floor(k / n) * c }))
  }
  const necks = new Set((model.info?.necks ?? []).map(n => n.stage))
  const queuedFor = (st: ColumnRole) => g.queued.filter(is => is.next === st)
  line(queuedFor('build'), machines.build.mx - 5, machines.build.mx - pad - 8, necks.has('build'))
  line(queuedFor('review'), machines.review.mx - 4, machines.review.mx - (machines.build.mx + machines.build.mw) - 8, necks.has('review'))
  line(queuedFor('audit'), machines.audit.mx - 4, machines.audit.mx - (machines.review.mx + machines.review.mw) - 8, necks.has('audit'))
  const gateX = cols[4]!.x + cols[4]!.w - 10
  line(g.approval, gateX - 6, gateX - (machines.audit.mx + machines.audit.mw) - 12)
  const tx = cols[5]!.x + 6, bedW = Math.max(c + 10, Math.round(36 * s)), cabW = Math.round(24 * s)
  g.merged.forEach(is => pos.set(is.id, { x: tx + bedW / 2, y: g0 + 3 * s }))

  const fy = 194, cb = Math.max(10, Math.round(c * 0.78)), bw = W - pad * 2
  const small = W < 620
  ctx.font = FONT(600, small ? 8.5 : 9.5)
  const bays: Bay[] = []; let bx = pad + 4; const plank = 14
  for (const r of LINE_REASONS) {
    const items = g.attention.filter(i => i.reason === r); if (!items.length) continue
    const ncol = Math.ceil(items.length / 3), label = small ? String(items.length) : items.length + ' ' + LINE_REASON_SHORT[r]
    const w = Math.max(ncol * (cb + 2) + 6, ctx.measureText(label).width + 10, GLYPH_SIZE(small) + 4)
    const x0 = bx + (w - ncol * (cb + 2)) / 2 + cb / 2 + 1
    items.forEach((is, k) => pos.set(is.id, { x: x0 + Math.floor(k / 3) * (cb + 2), y: fy - plank - (k % 3) * cb }))
    bays.push({ r, x: bx, w, label }); bx += w
  }
  const crateR = Math.max(bx + 4, pad + 70)
  // The crate's clickable area runs on over the beacon and its "N NEED YOU".
  ctx.font = FONT(600, small ? 9 : 10); spacing(ctx, '0.06em'); const needW = ctx.measureText('NEED YOU').width; spacing(ctx, '0px')
  ctx.font = FONT(600, small ? 17 : 20, 'mono'); const countW = ctx.measureText(String(g.attention.length)).width
  const needR = crateR + 20 * s + Math.max(needW, countW) + 4
  const areaY = fy - plank - cb * 3 - 6 - GLYPH_SIZE(small)
  const wx = small ? crateR + 66 : Math.max(crateR + 78, pad + bw * 0.56)
  // Two rows per bench; a long bench packs its columns closer to stay on screen.
  const pile = (arr: Item[], x0: number, right: number) => {
    const ncol = Math.ceil(arr.length / 2), stride = ncol > 1 ? clamp((right - x0 - cb - 8) / (ncol - 1), 3, cb + 3) : cb + 3
    arr.forEach((is, k) => pos.set(is.id, { x: x0 + cb / 2 + 4 + Math.floor(k / 2) * stride, y: fy - 5 - (k % 2) * cb }))
    return x0 + 8 + Math.max(1, ncol) * stride
  }
  // The bench labels must not run into each other on a phone.
  const waitW = sideLabelWidth(ctx, g.waiting.length, small ? 'wait' : 'waiting', small)
  const holdW = sideLabelWidth(ctx, g.hold.length, small ? 'hold' : 'on hold', small)
  const holdRoom = Math.max(holdW, Math.ceil(Math.max(1, g.hold.length) / 2) * (cb + 3) + 8)
  const wRight = pile(g.waiting, wx, Math.max(wx + cb + 8, W - pad - holdRoom - 8))
  let hx = small ? Math.max(wRight + 8, wx + waitW + 10) : Math.max(wRight + 16, wx + waitW + 16, pad + bw * 0.77)
  if (hx + holdW > W - pad) hx = Math.max(wx + waitW + 4, W - pad - holdW)
  pile(g.hold, hx, W - pad)
  return { pos, s, c, g0, gp, cols, machines, gateX, tx, bedW, cabW, fy, cb, bays, plank, crateX: pad, crateR, needR, areaY, wx, hx, pad, small }
}

// On a phone a bay is its count under its character; the hover card names it.
const GLYPH_SIZE = (small: boolean) => small ? 16 : 22

// Each bay's cubes wear its mood: puzzled by a question, sheepish and sooty
// after a failure, hopeful while waiting for a stamp, grumpy and bandaged
// when the setup is broken.
function bayFace(reason: AttentionReason | undefined, cb: number): CubeOptions {
  switch (reason) {
    case 'approach': return { eyes: 'worried', look: -cb * 0.06, lookY: cb * 0.04, soot: true }
    case 'question': return { eyes: 'puzzled', look: cb * 0.07, lookY: -cb * 0.05 }
    case 'approve': return { eyes: 'open', look: cb * 0.04, lookY: -cb * 0.07 }
    case 'failed': return { eyes: 'worried', look: 0, lookY: cb * 0.06, soot: true }
    case 'env': return { eyes: 'grumpy', look: 0, lookY: cb * 0.02, bandage: true }
    case 'verify': return { eyes: 'open', look: cb * 0.08, lookY: cb * 0.03 }
    default: return { eyes: 'open', look: -cb * 0.07, lookY: 0 }
  }
}

// One bay's character, drawn from the same paths as the SVG in the modal.
const glyphPaths = new Map<string, Path2D>()
function glyphPath(d: string) { let p = glyphPaths.get(d); if (!p) { p = new Path2D(d); glyphPaths.set(d, p) } return p }
function drawGlyph(ctx: CanvasRenderingContext2D, reason: AttentionReason, cx: number, bottom: number, size: number, t: number, pop: number, alpha: number) {
  const k = size / 24, m = glyphMotion(reason, t, reduced)
  const scale = 1 + 0.35 * Math.sin(Math.PI * clamp(pop, 0, 1)) * (pop < 1 ? 1 : 0)
  ctx.save(); ctx.globalAlpha = alpha
  ctx.translate(cx, bottom); ctx.scale(k * scale, k * scale); ctx.translate(-12, -24)
  ctx.lineCap = 'round'; ctx.lineJoin = 'round'
  const paint = (part: GlyphPart) => {
    const path = glyphPath(part.d)
    if (part.fill && part.fill !== 'none') { ctx.fillStyle = part.fill; ctx.fill(path) }
    if (part.stroke !== 'none') { ctx.strokeStyle = part.stroke ?? GLYPH_INK; ctx.lineWidth = part.width ?? 1.4; ctx.stroke(path) }
  }
  for (const part of BAY_GLYPHS[reason]) {
    if (!part.moves) { paint(part); continue }
    ctx.save(); ctx.translate(12 + m.dx, 12 + m.dy); ctx.rotate(m.turn); ctx.translate(-12, -12); paint(part); ctx.restore()
  }
  ctx.restore()
}
// When a cube last landed in each bay: its character pops.
const bayPop = new Map<AttentionReason, number>()
function sideLabelWidth(ctx: CanvasRenderingContext2D, n: number, t: string, small: boolean) {
  ctx.font = FONT(500, small ? 10 : 11, 'mono'); const a = ctx.measureText(String(n)).width
  ctx.font = FONT(600, small ? 9 : 10); spacing(ctx, '0.06em'); const b = ctx.measureText(' ' + t.toUpperCase()).width; spacing(ctx, '0px')
  return a + b
}

function sizeOf(item: Item, L: Layout) { return ['attention', 'waiting', 'hold'].includes(item.stage) ? L.cb : L.c }

function spawn(L: Layout, item: Item, p: { x: number; y: number }) {
  return ['attention', 'waiting', 'hold'].includes(item.stage) ? { x: p.x, y: 240 } : { x: -20, y: L.g0 }
}
function puff(x: number, y: number, col: string, n: number, size: number) {
  for (let i = 0; i < n; i++) toy.parts.push({ x: x + rand(-4, 4), y: y + rand(-3, 3), vx: rand(-22, 22), vy: rand(-28, -6), r: rand(2, 4) * size, g: rand(5, 9) * size, life: 0, max: rand(0.45, 0.8), col })
}
function onMove(from: string, to: string, v: Vis, now: number, L: Layout) {
  if (from === 'build' && to !== 'attention') puff(v.fx, v.fy - L.c * 0.6, '#c9d0d8', 7, 1)
  if (to === 'attention') puff(v.fx, v.fy - L.c * 0.6, '#f0b44c', 8, 1)
  if (to === 'merged') { toy.plus.push({ t: now }); model.lastMerge = now }
}
function truckDx(now: number, W: number, tx: number) {
  const t = now - toy.truckLeave, far = W - tx + 30
  if (t < 0 || t > 2300) return 0
  if (t < 900) return Math.pow(t / 900, 2) * far
  if (t < 1300) return far
  return Math.pow(1 - clamp((t - 1300) / 1000, 0, 1), 3) * far
}

function dimFor(is: Item) {
  const h = scene.hover
  if (h && h.kind === 'reason') return is.stage === 'attention' && is.reason === h.reason ? 1 : 0.3
  return 1
}
function blockerHover(is: Item) {
  const h = scene.hover; if (!h || h.kind !== 'issue') return false
  if (h.id === is.id) return true
  const hv = model.byId.get(h.id!)
  return !!hv && hv.stage === 'waiting' && lineBlocker(hv, model.byId)?.id === is.id
}
function tetherAlpha(w: LineItem, blocker: LineItem) {
  const h = scene.hover; if (!h || h.kind !== 'issue') return 0.2
  return (h.id === w.id || h.id === blocker.id) ? 0.85 : 0.08
}

// ---------------------------------------------------------------------------
// One frame
// ---------------------------------------------------------------------------
function frame(now: number, at: number) {
  const ctx = scene.ctx, { W, H } = scene
  if (!ctx || !W) return
  ctx.setTransform(scene.dpr, 0, 0, scene.dpr, 0, 0)
  ctx.clearRect(0, 0, W, H)
  // Merged cubes leave once the truck has taken them.
  const before = model.items.length
  model.items = model.items.filter(is => !(is.stage === 'merged' && now - (is.mergedAt ?? 0) > 3600))
  if (model.items.length !== before) regroup()
  const L = layout(ctx, W)
  publishSpots(L)
  for (const is of model.items) {
    const p = L.pos.get(is.id); if (!p) continue
    const st = lineStation(is)
    let v = vis.get(is.id)
    if (!v) {
      const fresh = model.fresh.has(is.id) && !scene.snap && !reduced
      const sp = fresh ? spawn(L, is, p) : p
      const fill = lineFill(is)
      v = { x: sp.x, y: sp.y, fx: sp.x, fy: sp.y, tx: p.x, ty: p.y, t0: now, dur: fresh ? 900 : 0, stage: st, hop: fresh, land: -1e9, k: fresh ? 0 : 1, dist: Math.hypot(p.x - sp.x, p.y - sp.y), size: sizeOf(is, L), col: fill, cf: fill, ct: fill, ct0: now, cdur: 0 }
      vis.set(is.id, v)
    }
    model.fresh.delete(is.id)
    v.size = sizeOf(is, L)
    const fill = lineFill(is)
    if (scene.snap || reduced) { v.x = v.fx = v.tx = p.x; v.y = v.fy = v.ty = p.y; v.dur = 0; v.k = 1; v.stage = st; v.hop = false; v.col = v.cf = v.ct = fill; continue }
    if (Math.abs(p.x - v.tx) > 0.5 || Math.abs(p.y - v.ty) > 0.5 || v.stage !== st) {
      const changed = v.stage !== st
      v.fx = v.x; v.fy = v.y; v.tx = p.x; v.ty = p.y; v.t0 = now
      v.dist = Math.hypot(v.tx - v.fx, v.ty - v.fy)
      v.dur = changed ? clamp(520 + v.dist * 2, 650, 1500) : 380
      v.hop = changed
      if (changed) onMove(v.stage, st, v, now, L)
      v.stage = st; v.loaded = 0
    }
    // A new colour blends over the hop that carries it, or briefly in place.
    if (fill !== v.ct) { v.cf = v.col; v.ct = fill; v.ct0 = now; v.cdur = v.t0 === now && v.dur ? v.dur : 380 }
    v.col = v.cdur ? lineMix(v.cf, v.ct, ease(clamp((now - v.ct0) / v.cdur, 0, 1))) : v.ct
    const k = v.dur ? clamp((now - v.t0) / v.dur, 0, 1) : 1
    const e = ease(k)
    v.x = v.fx + (v.tx - v.fx) * e; v.y = v.fy + (v.ty - v.fy) * e
    if (k >= 1 && v.k < 1 && v.hop) { v.land = now; if (is.stage === 'attention' && is.reason) bayPop.set(is.reason, now); if (is.stage !== 'merged') puff(v.x, v.y, '#5b626c', 3, 0.6) }
    v.k = k
  }
  scene.snap = false
  for (const id of vis.keys()) if (!model.byId.has(id)) vis.delete(id)
  const dt = Math.max(0, at - scene.lastAt); scene.lastAt = at
  scene.hits.length = 0
  draw(ctx, W, H, L, now, reduced ? 0 : at, dt)
  if (scene.hover?.kind === 'issue' && !model.byId.has(scene.hover.id!)) hideTip()
}

function draw(ctx: CanvasRenderingContext2D, W: number, H: number, L: Layout, now: number, at: number, dt: number) {
  const g = model.groups, inf = model.info, hits = scene.hits
  const { s, c, g0, cols, machines, gateX, tx, bedW, cabW, fy, cb, pad, small } = L
  const neckSet = new Set((inf?.necks ?? []).map(n => n.stage))
  const anyRunning = (inf?.running ?? 0) > 0
  const motion = !reduced

  // backdrop: wall and floor
  ctx.fillStyle = '#16191f'; ctx.fillRect(0, 0, W, g0 + 14 * s)
  ctx.fillStyle = '#121418'; ctx.fillRect(0, g0 + 14 * s, W, H)
  ctx.fillStyle = '#1c2026'; ctx.fillRect(0, g0 + 14 * s, W, 2)

  // headline on a little signboard
  const hsize = small ? 11.5 : 12.5
  ctx.font = FONT(500, hsize)
  const sep = '  ·  '
  const hw = parts.value.reduce((a, p) => a + ctx.measureText(p.t).width, 0) + ctx.measureText(sep).width * Math.max(0, parts.value.length - 1)
  rr(ctx, pad - 2, 6, hw + 16, 22, 6); ctx.fillStyle = '#1f242b'; ctx.fill(); ctx.lineWidth = 1.5; ctx.strokeStyle = INK; ctx.stroke()
  drawHeadline(ctx, pad + 6, 21, hsize)

  // conveyor: it rolls only while a worker is running
  const cx0 = pad, cx1 = tx - 6, ch = Math.max(7, 9 * s)
  rr(ctx, cx0, g0, cx1 - cx0, ch, ch / 2); ctx.fillStyle = '#2a2f37'; ctx.fill(); ctx.lineWidth = 1.6; ctx.strokeStyle = INK; ctx.stroke()
  const roll = anyRunning ? at * 5 : 0, wr = ch * 0.32, wstep = 15 * s
  for (let x = cx0 + ch; x < cx1 - ch / 2; x += wstep) {
    ctx.beginPath(); ctx.arc(x, g0 + ch / 2, wr, 0, Math.PI * 2); ctx.fillStyle = '#434a55'; ctx.fill()
    ctx.strokeStyle = '#20242a'; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(x - Math.cos(roll) * wr, g0 + ch / 2 - Math.sin(roll) * wr); ctx.lineTo(x + Math.cos(roll) * wr, g0 + ch / 2 + Math.sin(roll) * wr); ctx.stroke()
  }

  // machines
  const MH = Math.round(66 * s)
  const body: Record<ColumnRole, string> = { build: '#2f6f69', review: '#3b5a80', audit: '#6a5a48' }
  const bodyHi: Record<ColumnRole, string> = { build: '#3f8a83', review: '#4d72a0', audit: '#85725d' }
  for (const st of STAGES) {
    const M = machines[st], top = g0 - MH, blockH = MH - c - 12 * s
    const working = g[st].some(i => i.running)
    const hot = neckSet.has(st)
    rr(ctx, M.mx, top + 6, M.leg, MH - 6, 3); ctx.fillStyle = body[st]; ctx.fill(); ctx.lineWidth = 1.6; ctx.strokeStyle = INK; ctx.stroke()
    rr(ctx, M.mx + M.mw - M.leg, top + 6, M.leg, MH - 6, 3); ctx.fill(); ctx.stroke()
    if (st === 'build') {
      // chimney: smoke only while a build is running
      const chx = M.mx + M.mw - M.leg - 10 * s
      rr(ctx, chx, top - 10 * s, 8 * s, 14 * s, 2); ctx.fillStyle = '#28302f'; ctx.fill(); ctx.stroke()
      if (working && motion) { toy.smokeAcc += dt; if (toy.smokeAcc > 0.45) { toy.smokeAcc = 0; toy.parts.push({ x: chx + 4 * s, y: top - 10 * s, vx: rand(4, 10), vy: rand(-16, -10), r: 2.5 * s, g: 6 * s, life: 0, max: 1.4, col: '#7d858f' }) } }
      // pistons
      for (let j = 0; j < M.cap; j++) {
        const sx = M.mx + M.leg + L.gp + j * (c + L.gp) + c / 2
        const is = g.build[j], act = !!is && is.running
        const ext = act ? (Math.sin(at * 7 + j * 1.7) * 0.5 + 0.5) * 6 * s : 0
        ctx.fillStyle = '#9aa3ad'; ctx.fillRect(sx - 2 * s, top + blockH - 2, 4 * s, 4 * s + ext)
        rr(ctx, sx - c * 0.3, top + blockH + 2 * s + ext, c * 0.6, 4 * s, 1.5); ctx.fillStyle = '#c4ccd4'; ctx.fill(); ctx.lineWidth = 1.2; ctx.stroke()
      }
    }
    if (st === 'audit') {
      const ax = M.mx + M.mw / 2
      ctx.strokeStyle = INK; ctx.lineWidth = 1.6; ctx.beginPath(); ctx.moveTo(ax, top); ctx.lineTo(ax, top - 10 * s); ctx.stroke()
      ctx.beginPath(); ctx.arc(ax, top - 11 * s, 2.6 * s, 0, Math.PI * 2)
      ctx.fillStyle = working && (reduced || Math.sin(at * 6) > 0) ? COL.green : '#3b3f45'; ctx.fill(); ctx.stroke()
    }
    rr(ctx, M.mx - 3 * s, top, M.mw + 6 * s, blockH, 7 * s); ctx.fillStyle = body[st]; ctx.fill(); ctx.lineWidth = 1.6; ctx.strokeStyle = INK; ctx.stroke()
    rr(ctx, M.mx, top + 3, M.mw, 5 * s, 3); ctx.fillStyle = bodyHi[st]; ctx.fill()
    if (st === 'review') {
      // the reviewer's big eye: it reads while a review runs, sleeps when empty
      const ex = M.mx + M.mw * 0.5, ey = top + blockH * 0.5, er = Math.min(blockH * 0.32, 10 * s)
      ctx.beginPath(); ctx.arc(ex, ey, er, 0, Math.PI * 2); ctx.fillStyle = '#e9eef3'; ctx.fill(); ctx.lineWidth = 1.6; ctx.stroke()
      const look = working ? Math.sin(at * 2.2) * er * 0.45 : 0
      ctx.beginPath(); ctx.arc(ex + look, ey + (working ? er * 0.25 : 0), er * 0.42, 0, Math.PI * 2); ctx.fillStyle = '#12151a'; ctx.fill()
      if (!g.review.length) { ctx.beginPath(); ctx.moveTo(ex - er - 1, ey); ctx.arc(ex, ey, er + 1, Math.PI, 0); ctx.closePath(); ctx.fillStyle = body.review; ctx.fill(); ctx.stroke() }
    }
    // lamps: one per slot
    for (let j = 0; j < M.cap; j++) {
      if (st === 'build') continue
      const lx = M.mx + M.leg + L.gp + j * (c + L.gp) + c / 2, ly = top + blockH - 6 * s
      ctx.beginPath(); ctx.arc(lx, st === 'review' ? top + blockH - 4 * s : ly, Math.max(1.8, 2.4 * s), 0, Math.PI * 2)
      const on = j < g[st].length
      ctx.fillStyle = on ? COL.green : '#2a2f36'; if (on) { ctx.shadowColor = COL.green; ctx.shadowBlur = 6 } ctx.fill(); ctx.shadowBlur = 0
    }
    if (st === 'build') {
      for (let j = 0; j < M.cap; j++) {
        const lx = M.mx + 6 * s + j * 7 * s, ly = top + 12 * s
        ctx.beginPath(); ctx.arc(lx, ly, Math.max(1.8, 2.4 * s), 0, Math.PI * 2)
        const on = j < g.build.length; ctx.fillStyle = on ? COL.green : '#22302e'; if (on) { ctx.shadowColor = COL.green; ctx.shadowBlur = 6 } ctx.fill(); ctx.shadowBlur = 0
      }
    }
    // bottleneck: a FULL flag plus sweat drops
    if (hot) {
      ctx.font = FONT(600, small ? 8.5 : 9.5); spacing(ctx, '0.06em')
      const fw = ctx.measureText('FULL').width + 10, fh = 12, fxx = M.mx + (M.mw - fw) / 2 + (st === 'build' ? 4 * s : 0), fyy = top + blockH - fh - 3 * s
      const wob = Math.sin(at * 5) * 0.06
      ctx.save(); ctx.translate(fxx + fw / 2, fyy); ctx.rotate(wob)
      rr(ctx, -fw / 2, 0, fw, fh, 2); ctx.fillStyle = COL.coral; ctx.fill(); ctx.lineWidth = 1.4; ctx.strokeStyle = INK; ctx.stroke()
      ctx.fillStyle = '#1a0f0c'; ctx.textAlign = 'left'; ctx.fillText('FULL', -fw / 2 + 5, 9); ctx.restore(); spacing(ctx, '0px')
      hits.push({ kind: 'neck', stage: st, waiting: inf!.necks.find(nn => nn.stage === st)!.waiting, x: fxx, y: fyy, w: fw, h: fh })
      if (motion) {
        const dph = (at * 1.4) % 1
        ctx.globalAlpha = 1 - dph; ctx.fillStyle = '#9ccfe8'
        ctx.beginPath(); const dx = M.mx + M.mw + 6 * s, dy = top + 4 + dph * 14 * s; ctx.moveTo(dx, dy - 4 * s); ctx.quadraticCurveTo(dx + 3 * s, dy, dx, dy + 2 * s); ctx.quadraticCurveTo(dx - 3 * s, dy, dx, dy - 4 * s); ctx.fill(); ctx.globalAlpha = 1
      }
    }
  }

  // approval gate: the arm lifts when a merge goes through
  const gh = c + 16 * s
  rr(ctx, gateX, g0 - gh, 5 * s, gh, 2); ctx.fillStyle = '#5b6470'; ctx.fill(); ctx.lineWidth = 1.6; ctx.strokeStyle = INK; ctx.stroke()
  ctx.beginPath(); ctx.arc(gateX + 2.5 * s, g0 - gh - 3 * s, 3 * s, 0, Math.PI * 2); ctx.fillStyle = g.approval.length ? '#a9c8ea' : '#3a414b'; ctx.fill(); ctx.stroke()
  const lift = motion ? clamp(1 - Math.abs((now - model.lastMerge) - 450) / 650, 0, 1) : 0
  ctx.save(); ctx.translate(gateX + 2.5 * s, g0 - gh + 6 * s); ctx.rotate(lift * 1.25)
  const al = Math.max(24, (c + 4) * 2)
  rr(ctx, -al, -2.5 * s, al, 5 * s, 2); ctx.fillStyle = '#e4e9ef'; ctx.fill(); ctx.lineWidth = 1.3; ctx.stroke()
  ctx.fillStyle = '#c45b4a'; for (let x = -al + 4; x < -4; x += 10) ctx.fillRect(x, -2 * s, 4, 4 * s)
  ctx.restore()

  // truck
  if (g.merged.some(i => { const v = vis.get(i.id); return v && v.k >= 1 && !v.loaded })) {
    for (const i of g.merged) { const v = vis.get(i.id); if (v && v.k >= 1 && !v.loaded) v.loaded = now }
    if (now - toy.truckLeave > 2300) toy.truckLeave = now + 250
  }
  const dx = motion ? truckDx(now, W, tx) : 0
  const bump = motion ? clamp(1 - (now - model.lastMerge - 600) / 250, 0, 1) * (now - model.lastMerge > 600 ? 1 : 0) : 0
  const T = tx + dx, tyb = g0 + 3 * s - bump * 1.5
  ctx.fillStyle = '#1b1f25'; ctx.fillRect(tx - 6, g0 + ch + 3 * s, W - tx + 6, 3)
  rr(ctx, T, tyb - c * 0.9, 4 * s, c * 0.9 + 4, 2); ctx.fillStyle = '#3d8f63'; ctx.fill(); ctx.lineWidth = 1.6; ctx.strokeStyle = INK; ctx.stroke()
  for (const is of g.merged) {
    const v = vis.get(is.id); if (!v) continue
    let x = v.x, y = v.y, sy = 1, sx = 1
    if (v.k < 1 && v.hop) { y -= Math.sin(Math.PI * v.k) * clamp(v.dist * 0.25, 16, 44); sy = 1 + 0.12 * Math.sin(Math.PI * v.k); sx = 1 / sy }
    else { if (v.loaded && now - v.loaded > 1100) continue; x += dx; y = tyb }
    drawCube(ctx, x, y, c, v.col, { eyes: 'open', look: c * 0.06, sx, sy })
  }
  rr(ctx, T, tyb - c * 0.45, bedW, c * 0.45 + 5 * s, 3); ctx.fillStyle = '#4fa877'; ctx.fill(); ctx.lineWidth = 1.6; ctx.stroke()
  rr(ctx, T + bedW + 2, tyb - c - 6 * s, cabW, c + 11 * s, 5 * s); ctx.fillStyle = '#5cbf89'; ctx.fill(); ctx.stroke()
  rr(ctx, T + bedW + 2 + cabW * 0.35, tyb - c - 2 * s, cabW * 0.5, c * 0.45, 2); ctx.fillStyle = '#1c2a33'; ctx.fill(); ctx.lineWidth = 1.2; ctx.stroke()
  for (const wx of [T + 9 * s, T + bedW + cabW - 6 * s]) { ctx.beginPath(); ctx.arc(wx, tyb + 6 * s, 5.5 * s, 0, Math.PI * 2); ctx.fillStyle = '#1a1d22'; ctx.fill(); ctx.lineWidth = 1.6; ctx.stroke(); ctx.beginPath(); ctx.arc(wx, tyb + 6 * s, 2 * s, 0, Math.PI * 2); ctx.fillStyle = '#8a929c'; ctx.fill() }

  // tethers: a waiting cube is tied to the issue it waits for
  for (const w of g.waiting) {
    const blocker = lineBlocker(w, model.byId)
    const a = vis.get(w.id), bv = blocker && vis.get(blocker.id)
    if (a && bv && blocker) tether(ctx, a.x, a.y - cb, bv.x, bv.y + 2, tetherAlpha(w, blocker), '#a0a8b2')
  }

  // line cubes
  const bob = (i: number, amp: number, f: number) => motion ? -Math.max(0, Math.sin(at * f + i * 1.3)) * amp : 0
  for (const is of model.items) {
    if (['merged', 'attention', 'waiting', 'hold'].includes(is.stage)) continue
    const v = vis.get(is.id); if (!v) continue
    let x = v.x, y = v.y, sx = 1, sy = 1
    if (v.k < 1 && v.hop) { const hgt = clamp(v.dist * 0.25, 16, 44); y -= Math.sin(Math.PI * v.k) * hgt; sy = 1 + 0.12 * Math.sin(Math.PI * v.k); sx = 1 / sy }
    else if (motion) {
      const tl = now - v.land; if (tl < 260) { const q2 = 0.22 * (1 - tl / 260); sy = 1 - q2; sx = 1 + q2 * 0.8 }
    }
    const o: CubeOptions = { eyes: 'open', look: c * 0.06 }
    if (is.stage === 'queued') { if (is.next && neckSet.has(is.next)) { y += bob(is.order, 2.5 * s, 4); o.look = c * 0.09 } }
    else if (is.stage === 'approval') { o.look = c * 0.09; o.lookY = -c * 0.04 }
    else if (is.running) { o.lookY = c * 0.07; o.look = 0; if (is.stage === 'build' && motion) x += Math.sin(at * 14 + is.order) * 0.5 }
    else o.look = c * 0.09
    drawCube(ctx, x, y, c, v.col, { ...o, sx, sy })
    if (blockerHover(is)) { ctx.strokeStyle = COL.text; ctx.lineWidth = 1; rr(ctx, x - c / 2 - 3, y - c - 3, c + 6, c + 6, 5); ctx.stroke() }
    hits.push({ kind: 'issue', id: is.id, x: x - c / 2, y: y - c, w: c, h: c })
  }
  // audit scan beam over a running audit
  {
    const M = machines.audit
    if (g.audit.some(i => i.running)) { const by = g0 - c + (Math.sin(at * 3) * 0.5 + 0.5) * c; ctx.fillStyle = 'rgba(98,207,146,.35)'; ctx.fillRect(M.mx + M.leg, by, M.mw - M.leg * 2, 2) }
  }

  // station labels
  const ly = g0 + ch + 16 * s + 4
  const label = (x: number, txt: string, n: string | number, col: string) => {
    ctx.font = FONT(600, small ? 9 : 10); spacing(ctx, '0.06em'); ctx.fillStyle = COL.muted; ctx.textAlign = 'left'
    const T2 = txt.toUpperCase(); ctx.fillText(T2, x, ly); const w = ctx.measureText(T2).width; spacing(ctx, '0px')
    ctx.font = FONT(500, small ? 10 : 11, 'mono'); ctx.fillStyle = col; ctx.fillText(' ' + n, x + w, ly)
  }
  label(cols[0]!.x, small ? 'Q' : 'Queue', g.queued.length, neckSet.size ? COL.coral : COL.text)
  for (const [i, st] of STAGES.entries()) label(small ? cols[i + 1]!.x + 2 : machines[st].mx, small ? LINE_SHORT_LABELS[st].slice(0, 3) : LINE_SHORT_LABELS[st], g[st].length + '/' + model.slots[st], neckSet.has(st) ? COL.coral : g[st].length ? COL.text : COL.dim)
  label(cols[4]!.x + 4, small ? 'Appr' : 'Approval', g.approval.length, g.approval.length ? COL.text : COL.dim)
  label(tx, small ? 'Out' : 'Merged', model.merged, COL.green)

  // ---- bottom: the crate of issues that need the operator ----
  const attN = g.attention.length, pulse = motion ? 0.5 + 0.5 * Math.sin(at * 2.4) : 0.5
  const cx0b = L.crateX, cx1b = L.crateR, pk = L.plank
  if (attN) {
    const gr = ctx.createRadialGradient((cx0b + cx1b) / 2, fy - 22, 4, (cx0b + cx1b) / 2, fy - 22, (cx1b - cx0b) * 0.7 + 30)
    gr.addColorStop(0, `rgba(240,180,76,${0.15 + 0.1 * pulse})`); gr.addColorStop(1, 'rgba(240,180,76,0)')
    ctx.fillStyle = gr; ctx.fillRect(0, g0 + 16 * s, W, H)
  }
  rr(ctx, cx0b, fy - pk - cb * 1.3, cx1b - cx0b, cb * 1.3 + pk, 4); ctx.fillStyle = '#2b2419'; ctx.fill(); ctx.lineWidth = 1.6; ctx.strokeStyle = INK; ctx.stroke()
  hits.push({ kind: 'crate', x: cx0b, y: L.areaY, w: L.needR - cx0b, h: fy - L.areaY })
  let wi = 0
  for (const is of g.attention) {
    const v = vis.get(is.id); if (!v) continue
    const a = dimFor(is)
    const x = v.x; let y = v.y
    if (v.k < 1 && v.hop) y -= Math.sin(Math.PI * v.k) * clamp(v.dist * 0.2, 16, 40)
    else if (motion) y += -Math.pow(Math.max(0, Math.sin(at * 2.2 - wi * 0.55)), 10) * 3 * s
    drawCube(ctx, x, y, cb, v.col, { ...bayFace(is.reason, cb), alpha: a, glow: a === 1 ? 'rgba(240,180,76,.5)' : null, glowBlur: 4 + 6 * pulse })
    if (blockerHover(is)) { ctx.strokeStyle = COL.text; ctx.lineWidth = 1; rr(ctx, x - cb / 2 - 3, y - cb - 3, cb + 6, cb + 6, 4); ctx.stroke() }
    wi++
  }
  rr(ctx, cx0b - 2, fy - pk, cx1b - cx0b + 4, pk + 1, 3); ctx.fillStyle = attN ? '#8a6428' : '#3a3328'; ctx.fill(); ctx.lineWidth = 1.6; ctx.strokeStyle = INK; ctx.stroke()
  ctx.font = FONT(600, small ? 8.5 : 9.5); ctx.textAlign = 'left'
  L.bays.forEach((bay, i) => {
    const hov = scene.hover?.kind === 'reason' && scene.hover.reason === bay.r
    if (i) { ctx.fillStyle = 'rgba(10,12,15,.6)'; ctx.fillRect(bay.x, fy - pk + 2, 1.5, pk - 3) }
    ctx.fillStyle = hov ? '#fff4dc' : '#2a1d08'
    const tw2 = ctx.measureText(bay.label).width; ctx.fillText(bay.label, bay.x + (bay.w - tw2) / 2, fy - 3.5)
    // The bay's character stands on its top cube.
    const rows = Math.min(3, g.attention.filter(is => is.reason === bay.r).length)
    const glyph = GLYPH_SIZE(small)
    drawGlyph(ctx, bay.r, bay.x + bay.w / 2, fy - pk - rows * cb - 3, glyph, at, (now - (bayPop.get(bay.r) ?? -1e9)) / 450, scene.hover?.kind === 'reason' && scene.hover.reason !== bay.r ? 0.45 : 1)
    ctx.font = FONT(600, small ? 8.5 : 9.5); ctx.textAlign = 'left'
    hits.push({ kind: 'reason', reason: bay.r, x: bay.x, y: L.areaY, w: bay.w, h: fy - L.areaY })
  })
  // attention cubes are hit after their bay, so a cube wins over its bay
  for (const is of g.attention) {
    const v = vis.get(is.id); if (!v) continue
    hits.push({ kind: 'issue', id: is.id, x: v.x - cb / 2, y: v.y - cb, w: cb, h: cb })
  }
  // beacon
  const bxp = cx1b + 10 * s, byp = fy - pk - 4
  rr(ctx, bxp - 1.5, byp - 18 * s, 3, 18 * s + pk + 4, 1); ctx.fillStyle = '#4a4f57'; ctx.fill()
  if (attN && motion) {
    const ang = at * 3.2
    ctx.save(); ctx.translate(bxp, byp - 21 * s)
    ctx.fillStyle = 'rgba(240,180,76,.16)'
    for (const a0 of [ang, ang + Math.PI]) { ctx.beginPath(); ctx.moveTo(0, 0); ctx.arc(0, 0, 34 * s, a0 - 0.3, a0 + 0.3); ctx.closePath(); ctx.fill() }
    ctx.restore()
  }
  ctx.beginPath(); ctx.arc(bxp, byp - 19 * s, 5 * s, Math.PI, 0); ctx.lineTo(bxp + 5 * s, byp - 17 * s); ctx.lineTo(bxp - 5 * s, byp - 17 * s); ctx.closePath()
  ctx.fillStyle = attN ? (pulse > 0.5 ? '#ffd27a' : '#f0b44c') : '#3a3f46'; ctx.fill(); ctx.lineWidth = 1.4; ctx.strokeStyle = INK; ctx.stroke()
  const nx = bxp + 10 * s
  ctx.font = FONT(600, small ? 17 : 20, 'mono'); ctx.fillStyle = attN ? COL.amber : COL.dim; ctx.fillText(String(attN), nx, fy - 18)
  ctx.font = FONT(600, small ? 9 : 10); spacing(ctx, '0.06em'); ctx.fillStyle = attN ? '#d9a44a' : COL.dim; ctx.fillText('NEED YOU', nx, fy - 4); spacing(ctx, '0px')

  // benches: waiting on other issues, and on hold (asleep)
  const bench = (x0: number, n: number, col: string) => { const w = Math.min(Math.ceil(Math.max(1, n) / 2) * (cb + 3) + 6, W - pad - x0); rr(ctx, x0, fy - 5, w, 5, 2); ctx.fillStyle = col; ctx.fill(); ctx.lineWidth = 1.4; ctx.strokeStyle = INK; ctx.stroke(); return w }
  bench(L.wx, g.waiting.length, '#3a3f47'); const hw2 = bench(L.hx, g.hold.length, '#2b2f35')
  for (const st of ['waiting', 'hold'] as const) {
    for (const is of g[st]) {
      const v = vis.get(is.id); if (!v) continue
      let y = v.y
      if (v.k < 1 && v.hop) y -= Math.sin(Math.PI * v.k) * clamp(v.dist * 0.2, 16, 40)
      drawCube(ctx, v.x, y, cb, v.col, st === 'waiting' ? { eyes: 'open', look: cb * 0.1, lookY: -cb * 0.05, eyeCol: '#14171b' } : { eyes: 'closed', eyeCol: '#1a1d22' })
      if (blockerHover(is)) { ctx.strokeStyle = COL.text; ctx.lineWidth = 1; rr(ctx, v.x - cb / 2 - 3, y - cb - 3, cb + 6, cb + 6, 4); ctx.stroke() }
      hits.push({ kind: 'issue', id: is.id, x: v.x - cb / 2, y: y - cb, w: cb, h: cb })
    }
  }
  const sl = (x: number, n: number, t: string, col: string) => { ctx.font = FONT(500, small ? 10 : 11, 'mono'); ctx.fillStyle = col; ctx.fillText(String(n), x, fy - cb * 2 - 12); const w = ctx.measureText(String(n)).width; ctx.font = FONT(600, small ? 9 : 10); spacing(ctx, '0.06em'); ctx.fillStyle = COL.dim; ctx.fillText(' ' + t.toUpperCase(), x + w, fy - cb * 2 - 12); spacing(ctx, '0px') }
  sl(L.wx, g.waiting.length, small ? 'wait' : 'waiting', COL.muted); sl(L.hx, g.hold.length, small ? 'hold' : 'on hold', COL.dim)
  if (g.hold.length && motion) {
    const zt = (at * 0.6) % 1, zx = Math.min(W - 10, Math.max(L.hx + hw2 - 4, L.hx + sideLabelWidth(ctx, g.hold.length, small ? 'hold' : 'on hold', small) + 4))
    ctx.globalAlpha = Math.sin(zt * Math.PI) * 0.7; ctx.font = FONT(600, 10 + zt * 3); ctx.fillStyle = COL.muted; ctx.fillText('z', zx + zt * 6, fy - cb * 2 - 6 - zt * 12); ctx.globalAlpha = 1
  }

  // cubes that left the line fade where they stood
  for (let i = ghosts.length - 1; i >= 0; i--) {
    const gh = ghosts[i]!, k = (now - gh.t0) / 600
    if (k >= 1) { ghosts.splice(i, 1); continue }
    drawCube(ctx, gh.x, gh.y + k * 4, gh.size, gh.fill, { eyes: 'closed', alpha: 1 - k })
  }
  // particles
  for (let i = toy.parts.length - 1; i >= 0; i--) {
    const p = toy.parts[i]!; p.life += dt; if (p.life > p.max) { toy.parts.splice(i, 1); continue }
    p.x += p.vx * dt; p.y += p.vy * dt; p.vx *= 0.96; const k = p.life / p.max
    ctx.globalAlpha = (1 - k) * 0.75; ctx.fillStyle = p.col; ctx.beginPath(); ctx.arc(p.x, p.y, p.r + p.g * k, 0, Math.PI * 2); ctx.fill()
  }
  ctx.globalAlpha = 1
  // +1 floaters over the truck
  for (let i = toy.plus.length - 1; i >= 0; i--) {
    const t = (now - toy.plus[i]!.t) / 1400; if (t > 1) { toy.plus.splice(i, 1); continue }
    if (t < 0.45) continue
    const k = (t - 0.45) / 0.55
    ctx.globalAlpha = 1 - k; ctx.font = FONT(600, 12, 'mono'); ctx.fillStyle = COL.green; ctx.fillText('+1', tx + bedW / 2 - 6, g0 - c - 14 - k * 16); ctx.globalAlpha = 1
  }
}

// The resting place of every cube, published only when the layout changes.
function publishSpots(L: Layout) {
  const crate = { x: Math.round(L.crateX), y: Math.round(L.areaY), w: Math.round(L.needR - L.crateX), h: Math.round(L.fy - L.areaY) }
  const bays = L.bays.map(bay => ({ reason: bay.r, x: Math.round(bay.x), y: crate.y, w: Math.round(bay.w), h: crate.h }))
  const key = scene.W + '|' + JSON.stringify([crate, bays]) + '|' + [...L.pos.entries()].map(([id, p]) => `${id}:${Math.round(p.x)},${Math.round(p.y)}`).join(';')
  if (key === layoutKey) return
  layoutKey = key
  const next: Record<string, { x: number; y: number }> = {}
  for (const [id, p] of L.pos) {
    const item = model.byId.get(id)
    if (!item || item.stage === 'merged') continue
    const size = sizeOf(item, L)
    next[id] = { x: Math.round(p.x), y: Math.round(p.y - size / 2) }
  }
  spots.value = next
  regions.value = { crate, bays }
}

// ---------------------------------------------------------------------------
// Pointer: tooltips, open an issue, jump to Needs attention
// ---------------------------------------------------------------------------
function hitAt(x: number, y: number): Hit | null {
  for (let i = scene.hits.length - 1; i >= 0; i--) { const h = scene.hits[i]!; if (x >= h.x && x <= h.x + h.w && y >= h.y && y <= h.y + h.h) return h }
  return null
}
const hitKey = (h: Hit) => h.kind === 'issue' ? `issue:${h.id}` : h.kind === 'reason' ? `reason:${h.reason}` : h.kind === 'neck' ? `neck:${h.stage}` : 'crate'
function hideTip() { tip.value = null; tipAt.value = null; scene.hover = null; if (canvas.value) canvas.value.style.cursor = '' }
function cardFor(h: Hit): LineCard | null {
  if (h.kind === 'issue') {
    const is = model.byId.get(h.id!); if (!is) return null
    return lineIssueCard(is, rawIssues.value.get(is.id), unblocks.value.get(is.id) ?? 0, true)
  }
  if (h.kind === 'reason') return lineBayCard(h.reason!, model.groups.attention)
  if (h.kind === 'crate') {
    const n = model.groups.attention.length
    return lineListCard(n ? `${n} need${n === 1 ? 's' : ''} you` : 'Nothing needs you', crateRows.value.map(row => row.item), n ? 'Click to see them all' : '')
  }
  return lineListCard(`${LINE_SHORT_LABELS[h.stage!]} is full`, [], '', `${h.waiting} waiting in front of it`)
}
function showTip(h: Hit | null) {
  if (!h) { hideTip(); requestDraw(); return }
  scene.hover = h
  if (canvas.value) canvas.value.style.cursor = h.kind === 'neck' ? 'default' : 'pointer'
  const card = cardFor(h)
  if (!card) { hideTip(); return }
  const key = hitKey(h)
  if (tip.value?.key !== key) {
    const r = canvas.value!.getBoundingClientRect()
    tip.value = { key, card, anchor: { x: r.left + h.x, y: r.top + h.y, w: h.w, h: h.h } }
    tipAt.value = null
    void nextTick(placeTip)
  }
  requestDraw()
}
// Measured once rendered, then clamped to the viewport and flipped as needed.
function placeTip() {
  const el = tipEl.value, t = tip.value
  if (!el || !t) return
  const at = placeCard(t.anchor, { w: el.offsetWidth, h: el.offsetHeight }, { w: document.documentElement.clientWidth, h: window.innerHeight })
  tipAt.value = { left: at.left, top: at.top }
}
function point(e: PointerEvent | MouseEvent) {
  const r = canvas.value!.getBoundingClientRect()
  return { x: e.clientX - r.left, y: e.clientY - r.top }
}
// On a touch screen the first tap shows the card and a second tap on the
// same thing opens it.
let touch = false, armed = false
function onPointer(e: PointerEvent) {
  const p = point(e), h = hitAt(p.x, p.y)
  if (e.type === 'pointerdown') {
    touch = e.pointerType === 'touch' || e.pointerType === 'pen'
    armed = !!h && tip.value?.key === hitKey(h)
  }
  showTip(h)
}
function onLeave(e: PointerEvent) { if (e.pointerType === 'mouse') hideTip() }
function onClick(e: MouseEvent) {
  const p = point(e), h = hitAt(p.x, p.y)
  if (!h) { hideTip(); return }
  if (touch && !armed && h.kind !== 'neck') return
  touch = false; armed = false
  activate(h)
}
function activate(h: Hit) {
  if (h.kind === 'issue') { const is = model.byId.get(h.id!); if (is) { hideTip(); emit('open', is.card) } }
  else if (h.kind === 'reason') { hideTip(); emit('attention', h.reason!) }
  else if (h.kind === 'crate') { hideTip(); emit('attention', 'all') }
}
// The crate and bay buttons are transparent and let the pointer through to
// the canvas; they are there for the keyboard and for screen readers.
function focusRegion(rect: Rect, kind: 'crate' | 'reason', reason?: AttentionReason) {
  showTip({ kind, reason, x: rect.x, y: rect.y, w: rect.w, h: rect.h })
}
function blurRegion() { hideTip(); requestDraw() }
function onOutside(e: PointerEvent) { if (tip.value && e.target !== canvas.value) hideTip() }
function onScroll() { if (tip.value) hideTip() }
function openLink(item: LineItem) { emit('open', item.card) }

// ---------------------------------------------------------------------------
// Loop: runs only while the strip is on screen, the tab is visible, and
// motion is allowed. A still frame is redrawn on demand.
// ---------------------------------------------------------------------------
let raf = 0, last = 0, at = 0, onScreen = true, pending = false
let resizeObserver: ResizeObserver | null = null
let intersectionObserver: IntersectionObserver | null = null
let motionQuery: MediaQueryList | null = null

const animating = () => !reduced && onScreen && document.visibilityState === 'visible'
function tick(t: number) {
  raf = 0
  const dt = last ? Math.min(0.1, (t - last) / 1000) : 0; last = t
  at += dt
  frame(t, at)
  if (animating()) raf = requestAnimationFrame(tick)
}
function start() { if (!raf && animating()) { last = 0; raf = requestAnimationFrame(tick) } }
function stop() { if (raf) cancelAnimationFrame(raf); raf = 0 }
function requestDraw() {
  if (!import.meta.client || raf || pending) return
  pending = true
  requestAnimationFrame((t) => { pending = false; if (!raf) frame(t, at) })
}
function resize() {
  const el = canvas.value; if (!el) return
  const r = el.getBoundingClientRect()
  const dpr = Math.min(2, window.devicePixelRatio || 1)
  scene.W = r.width; scene.H = r.height || 200; scene.dpr = dpr
  el.width = Math.round(r.width * dpr); el.height = Math.round(scene.H * dpr)
  scene.snap = true
  requestDraw()
}
function onVisibility() { if (animating()) start(); else stop() }
function onMotion() { reduced = !!motionQuery?.matches; scene.snap = true; if (reduced) stop(); else start(); requestDraw() }
function readTokens() {
  const css = getComputedStyle(document.documentElement)
  const token = (name: string, fallback: string) => css.getPropertyValue(name).trim() || fallback
  COL.text = token('--ui-text', COL.text); COL.muted = token('--color-muted', COL.muted)
  COL.green = token('--color-mint-400', COL.green); COL.amber = token('--color-amber-200', COL.amber)
  COL.coral = token('--color-coral', COL.coral); COL.panel = token('--color-panel', COL.panel); COL.line = token('--color-line', COL.line)
  SANS = getComputedStyle(canvas.value!).fontFamily || SANS
}

onMounted(() => {
  const el = canvas.value!
  scene.ctx = el.getContext('2d')
  motionQuery = window.matchMedia('(prefers-reduced-motion: reduce)')
  reduced = motionQuery.matches
  motionQuery.addEventListener('change', onMotion)
  readTokens()
  resizeObserver = new ResizeObserver(resize); resizeObserver.observe(el)
  intersectionObserver = new IntersectionObserver(([entry]) => { onScreen = !!entry?.isIntersecting; onVisibility() })
  intersectionObserver.observe(el)
  document.addEventListener('visibilitychange', onVisibility)
  document.addEventListener('pointerdown', onOutside, true)
  window.addEventListener('scroll', onScroll, { passive: true, capture: true })
  window.addEventListener('resize', onScroll)
  apply(state.value)
  resize()
  start()
  document.fonts?.ready.then(() => { scene.snap = true; requestDraw() })
})
onBeforeUnmount(() => {
  stop()
  resizeObserver?.disconnect()
  intersectionObserver?.disconnect()
  document.removeEventListener('visibilitychange', onVisibility)
  document.removeEventListener('pointerdown', onOutside, true)
  window.removeEventListener('scroll', onScroll, { capture: true })
  window.removeEventListener('resize', onScroll)
  motionQuery?.removeEventListener('change', onMotion)
  vis.clear(); ghosts.length = 0; toy.parts.length = 0; toy.plus.length = 0
})
</script>

<template>
  <section class="factory-line relative overflow-hidden rounded-xl border border-line bg-panel" aria-label="Factory line">
    <canvas
      ref="canvas"
      role="img"
      :aria-label="label"
      class="block h-[200px] w-full touch-manipulation"
      @pointermove="onPointer"
      @pointerdown="onPointer"
      @pointerleave="onLeave"
      @click="onClick"
    />
    <!-- Keyboard and screen-reader ways into the crate. Transparent, and the
         pointer goes through them to the canvas underneath. -->
    <button
      v-if="regions.crate.w"
      type="button"
      class="line-region pointer-events-none absolute rounded-md opacity-0 focus-visible:opacity-100"
      :style="{ left: regions.crate.x + 'px', top: regions.crate.y + 'px', width: regions.crate.w + 'px', height: regions.crate.h + 'px' }"
      :aria-label="`Show the ${needCount} issue${needCount === 1 ? '' : 's'} that need${needCount === 1 ? 's' : ''} you`"
      aria-haspopup="dialog"
      data-line-crate
      @focus="focusRegion(regions.crate, 'crate')"
      @blur="blurRegion"
      @click="emit('attention', 'all')"
    />
    <button
      v-for="bay in regions.bays"
      :key="bay.reason"
      type="button"
      class="line-region pointer-events-none absolute rounded-md opacity-0 focus-visible:opacity-100"
      :style="{ left: bay.x + 'px', top: bay.y + 'px', width: bay.w + 'px', height: bay.h + 'px' }"
      :aria-label="`Show the ${info?.reasons[bay.reason] ?? 0} in ${LINE_REASON_SHORT[bay.reason]}: ${LINE_REASON_TEXT[bay.reason]}`"
      aria-haspopup="dialog"
      :data-line-bay="bay.reason"
      @focus="focusRegion(bay, 'reason', bay.reason)"
      @blur="blurRegion"
      @click="emit('attention', bay.reason)"
    />
    <Teleport to="body">
      <div
        v-if="tip"
        ref="tipEl"
        class="factory-card pointer-events-none fixed z-50 w-max max-w-[min(280px,calc(100vw-16px))] rounded-lg border border-line px-3 py-2.5 text-xs text-white"
        :style="{ left: (tipAt?.left ?? 0) + 'px', top: (tipAt?.top ?? 0) + 'px', visibility: tipAt ? 'visible' : 'hidden' }"
        aria-hidden="true"
        data-line-card
      >
        <template v-if="tip.card.kind === 'issue'">
          <p class="text-muted tabular-nums">{{ tip.card.ref }}</p>
          <p class="mt-0.5 line-clamp-2 text-sm font-semibold leading-5" data-card-title>{{ tip.card.title }}</p>
          <p class="mt-1 flex items-start gap-1.5 text-white/90" data-card-status><BayGlyph v-if="tip.card.reason" :reason="tip.card.reason" :size="16" /><span>{{ tip.card.status }}</span></p>
          <p v-if="tip.card.detail" class="mt-0.5 line-clamp-2 text-muted">{{ tip.card.detail }}</p>
          <p class="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1">
            <span class="inline-flex items-center gap-1.5 text-muted"><span class="inline-block size-2.5 rounded-sm border border-black/40" :style="{ background: tip.card.swatch }" />{{ tip.card.stage }}</span>
            <span v-if="tip.card.unblocks > 0" class="rounded-full border border-amber-200/40 px-1.5 text-[11px] text-amber-200" data-card-unblocks>Unblocks {{ tip.card.unblocks }}</span>
          </p>
          <p class="mt-1.5 text-[11px] text-muted">{{ tip.card.hint }}</p>
        </template>
        <template v-else>
          <p class="flex items-center gap-1.5 font-semibold"><BayGlyph v-if="tip.card.reason" :reason="tip.card.reason" :size="18" />{{ tip.card.heading }}</p>
          <p v-if="tip.card.sub" class="mt-0.5 text-white/85">{{ tip.card.sub }}</p>
          <ul v-if="tip.card.rows.length" class="mt-1 flex flex-col gap-0.5">
            <li v-for="row in tip.card.rows" :key="row.id" class="flex min-w-0 gap-1.5">
              <span class="shrink-0 tabular-nums text-muted">{{ row.ref }}</span><span class="truncate">{{ row.title }}</span>
            </li>
          </ul>
          <p v-if="tip.card.more" class="mt-0.5 text-muted">+{{ tip.card.more }} more</p>
          <p v-if="tip.card.hint" class="mt-1.5 text-[11px] text-muted">{{ tip.card.hint }}</p>
        </template>
      </div>
    </Teleport>
    <div class="sr-only" data-line-summary>{{ summary }}</div>
    <ul class="sr-only" aria-label="Issues on the factory line">
      <li v-for="link in links" :key="link.item.id">
        <a
          :href="link.href"
          tabindex="-1"
          :data-line-issue="link.item.id"
          :data-x="spots[link.item.id]?.x"
          :data-y="spots[link.item.id]?.y"
          @click.prevent="openLink(link.item)"
        >{{ link.text }}</a>
      </li>
    </ul>
  </section>
</template>

<style scoped>
.factory-card { background: #0b0d10; box-shadow: 0 6px 20px rgb(0 0 0 / 0.45); }
.line-region { outline: 2px solid var(--color-sky-200, #a5d8ff); outline-offset: 1px; }
</style>
